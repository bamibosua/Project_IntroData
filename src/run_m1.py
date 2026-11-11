"""Run entry, source download and references together as papers become ready."""
from itertools import chain
import logging
from pathlib import Path
import signal
import sys
import threading
import time

if __package__:
    from .entry_discovery import run_entry_discovery as entry
    from .entry_discovery.arxiv_cooldown import SharedCooldown
    from .entry_discovery.discovery import ArxivClient
    from .downloader.client import FastArxivClient
    from .downloader.runner import download_stream
    from .references import config, crawl_references as references
    from .references.s2_client import S2Client
else:
    from entry_discovery import run_entry_discovery as entry
    from entry_discovery.arxiv_cooldown import SharedCooldown
    from entry_discovery.discovery import ArxivClient
    from downloader.client import FastArxivClient
    from downloader.runner import download_stream
    from references import config, crawl_references as references
    from references.s2_client import S2Client

logger = logging.getLogger(__name__)


def main(argv=None, *, stop_event=None):
    argv = sys.argv[1:] if argv is None else argv
    if any(arg in ('-h', '--help') for arg in argv):
        return entry.main(argv)
    stop = stop_event if stop_event is not None else threading.Event()
    done, status = threading.Event(), {'exit_code': None}
    publications, publications_lock = {}, threading.Lock()
    references_state = {"exit_code": 0, "reason": None, "buffered": 0}
    codes, results, threads = {}, {}, {}
    shared = SharedCooldown()
    interrupted = False
    old_handler = None
    s2 = None

    def finish(code):
        status['exit_code'] = code
        done.set()

    def interrupt(signum, frame):
        nonlocal interrupted
        if not interrupted:
            logger.info('[PIPELINE] Stopping; keeping saved results.')
        interrupted = True
        stop.set()

    def publish(jobs):
        with publications_lock:
            publications.update((job['arxiv_id'], list(job['versions'])) for job in jobs)

    def reference_jobs():
        seen, next_notice = set(), 0.0
        while True:
            if stop.is_set():
                references_state.update(exit_code=130, reason='interrupted')
                return
            finished = done.is_set()
            if finished and status['exit_code'] != 0:
                references_state.update(exit_code=1, reason='entry_failed')
                return
            with publications_lock:
                snapshot = publications.copy()
            for aid, versions in snapshot.items():
                if stop.is_set():
                    references_state.update(exit_code=130, reason='interrupted')
                    return
                if done.is_set() and status['exit_code'] != 0:
                    references_state.update(exit_code=1, reason='entry_failed')
                    return
                if aid not in seen:
                    seen.add(aid)
                    next_notice = 0.0
                    yield {'arxiv_id': aid, 'versions': versions}
            if finished:
                return
            if time.monotonic() >= next_notice:
                logger.info('[REFERENCES] Waiting for entry: %d/200 buffered.', references_state['buffered'])
                next_notice = time.monotonic() + 60
            stop.wait(1)

    def download_jobs():
        seen, next_notice = set(), 0.0
        while not stop.is_set():
            finished = done.is_set()
            if finished and status['exit_code'] != 0:
                stop.set()
                return
            with publications_lock:
                snapshot = publications.copy()
            for aid, versions in snapshot.items():
                if stop.is_set():
                    return
                if done.is_set() and status['exit_code'] != 0:
                    stop.set()
                    return
                if aid not in seen:
                    seen.add(aid)
                    next_notice = 0.0
                    yield aid, versions
            if finished:
                return
            if time.monotonic() >= next_notice:
                logger.info('[DOWNLOAD] Waiting for entry.')
                next_notice = time.monotonic() + 60
            # Let the downloader collect completed papers while entry is idle.
            yield None
            stop.wait(1)

    def download():
        jobs = download_jobs()
        for first in jobs:
            if first is not None:
                break
        else:
            return {'exit_code': 130 if stop.is_set() else 0, 'reason': 'stopped' if stop.is_set() else None}
        if stop.is_set():
            return {'exit_code': 130, 'reason': 'interrupted'}
        source = FastArxivClient(delay=0.6, shared_cooldown=shared, stop_event=stop)
        logger.info('[DOWNLOAD] Starting from ready papers.')
        return download_stream(chain((first,), jobs), output_dir=Path(args.out).parent,
                               client=source, stop_event=stop)

    def worker(name, target):
        code = 1
        try:
            result = target()
            results[name] = result
            code = result if isinstance(result, int) else result['exit_code']
            if name != 'ENTRY' and not done.is_set() and not stop.is_set():
                code = 1
                logger.error('[%s] Stopped before entry finished.', name)
                stop.set()
            if code and (name == 'ENTRY' or result.get('reason') != 'publications_incomplete'):
                stop.set()
        except BaseException as exc:
            code = 1
            logger.error('[%s] Failed: %s.', name, type(exc).__name__)
            stop.set()
        finally:
            codes[name] = code
            if name == 'ENTRY' and not done.is_set():
                finish(code)

    try:
        if threading.current_thread() is threading.main_thread():
            old_handler = signal.signal(signal.SIGINT, interrupt)
        config.load_env()
        key = config.get_api_key()
        logger.info('[PIPELINE] Starting entry, download and references.')
        logger.info('[REFERENCES] API key loaded.' if key else '[REFERENCES] No API key; using public access.')
        metadata = ArxivClient(delay=3.5, retries=2, shared_cooldown=shared, stop_event=stop)
        s2 = S2Client(api_key=key, stop_event=stop)
        args = references.parse_args([])
        targets = {
            'REFERENCES': lambda: references.run(args, client=s2, source=reference_jobs(),
                                                  stream_status=references_state),
            'DOWNLOAD': download,
            'ENTRY': lambda: entry.main(argv, client=metadata, on_batch=publish, on_finish=finish, stop_event=stop),
        }
        for name, target in targets.items():
            threads[name] = threading.Thread(target=worker, args=(name, target), name=f'm1-{name.lower()}')
        for thread in threads.values():
            thread.start()
        next_notice = 0.0
        while True:
            if not any(thread.is_alive() for thread in threads.values()):
                break
            remaining = shared.remaining()
            if remaining > 0 and time.monotonic() >= next_notice:
                logger.info('[ARXIV] Shared cooldown: %.0fs remaining.', remaining)
                next_notice = time.monotonic() + 60
            for thread in threads.values():
                thread.join(0.2)
    except KeyboardInterrupt:
        interrupt(signal.SIGINT, None)
    except Exception as exc:
        logger.error('[PIPELINE] Failed: %s.', type(exc).__name__)
        stop.set()
    finally:
        if not done.is_set():
            finish(1)
        for thread in threads.values():
            if thread.ident is not None:
                thread.join()
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
        if s2 is not None:
            try:
                s2.close()
            except Exception:
                logger.warning('[REFERENCES] Client close failed.')
    if interrupted:
        return 130
    failed = len(codes) != 3 or any(codes.values())
    ref_report = results.get('REFERENCES', {})
    warnings = bool(ref_report.get('papers', {}).get('count_mismatch') or ref_report.get('warnings'))
    logger.info('[PIPELINE] Finished%s.', ' with errors' if failed else ' with warnings' if warnings else '')
    return 1 if failed else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s', force=True)
    raise SystemExit(main())
