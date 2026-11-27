"""Recheck previously failed ok inputs without replacing the cleaned dataset."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import time
import traceback
import re
from collections import Counter

import cleanup_leaves as cleanup

ROOT = Path(__file__).resolve().parents[2]


def failure_note(error, context, merged):
    """Report observed evidence separately from unverified underlying causes."""
    span = context.get('source_segment_span', [0, len(merged)])
    segment = merged[span[0]:span[1]]
    snippet = context.get('token_context', '')
    location = merged.find(snippet, span[0], span[1]) if snippet else -1
    precision = 'exact_context' if location >= 0 else 'segment_start'
    if location < 0: location = span[0]
    line = merged.count('\n', 0, location) + 1
    evidence = []
    confidence = 'symptom_confirmed_root_cause_unverified'
    if re.search(r'\\(?:if[a-zA-Z@]*|else|fi)\b', segment):
        evidence.append('Đoạn source có conditional TeX; chưa xác định nhánh nào được thực thi.')
    if r'\$' in snippet:
        evidence.append(r'Gần vị trí lỗi có \$ (dấu dollar literal), không tự đổi thành dấu đóng toán.')
    if error.startswith('Unclosed math environment:'):
        group = 'math_environment_requires_review'
        note = ('Scanner không xác định được điểm đóng môi trường toán trong đoạn đang xử lý. '
                'Cần kiểm tra macro, nesting và ranh giới body_segments trước khi sửa.')
    elif error.startswith('Unexpected inline math opener'):
        group = 'nested_inline_math_requires_review'
        note = (r'Scanner gặp \( khi công thức inline trước chưa đóng. '
                'Cần kiểm tra phạm vi macro/nhóm và nhánh conditional; chưa kết luận source hỏng.')
    elif '$$' in error:
        group = 'display_dollar_requires_review'
        note = ('Scanner chưa ghép được $$ trong đoạn đang xử lý. '
                'Cần phân biệt display math, ô bảng rỗng và nội dung literal; không tự thêm/xóa dollar.')
    elif 'math delimiter' in error:
        group = 'inline_dollar_requires_review'
        note = ('Scanner chưa ghép được dấu toán inline trong đoạn đang xử lý. '
                'Cần đối chiếu source, macro và ranh giới body_segments; chưa chứng minh lỗi biên dịch LaTeX.')
    else:
        group = 'other_requires_review'
        note = 'Chưa có cách sửa đã xác minh cho lỗi này: ' + error
    if error.startswith('Unclosed math environment:'):
        env = error.split(':', 1)[1].strip()
        # Confirm the narrow, common signature without interpreting arbitrary
        # parameterized TeX. These macros require substitution of #1, not a
        # zero-argument delimiter alias.
        for declaration in cleanup.BODY_DEFINITION_RE.finditer(merged):
            end = cleanup.definition_end(merged, declaration.start())
            if end is None: continue
            source = merged[declaration.start():end]
            name = re.match(
                r'\\[A-Za-z@]+\*?\s*(?:\{\s*(\\[A-Za-z@]+)\s*\}|(\\[A-Za-z@]+))', source)
            if not name: continue
            command = name.group(1) or name.group(2)
            body = source[name.end():]
            signature = re.fullmatch(
                r'\s*(?:\[1\]|#1)\s*\{\s*\\label\{#1\}\s*\\end\s*\{'
                + re.escape(env) + r'\}\s*\}', body)
            if signature and re.search(re.escape(command) + r'(?![A-Za-z@])\s*\{', segment):
                group = 'parameterized_closing_macro'
                note = (f'Cleanup chưa mở rộng macro có tham số {command}: macro nhận label và đóng '
                        f'môi trường {env}. Bộ alias hiện chỉ hỗ trợ delimiter không có tham số. '
                        'Cần thêm phép thay thế tham số có kiểm soát; không sửa source bằng đoán nghĩa tên macro.')
                evidence.append('Khai báo xác nhận: ' + source)
                confidence = 'confirmed_scanner_limitation'
                break
        if confidence != 'confirmed_scanner_limitation':
            for option in re.finditer(r'\\\\\s*\[', segment):
                argument = cleanup.arg_at(segment, option.end() - 1, '[', ']')
                if argument and re.search(r'\\end\s*\{' + re.escape(env) + r'\}', argument[0]):
                    group = 'rowbreak_optional_argument_ambiguity'
                    note = (r'Scanner đọc dấu [ sau \\ thành tham số giãn dòng và nuốt qua dấu đóng môi trường. '
                            'Source tại đây có khoảng dạng [a,b); cần kiểm tra cách TeX thực thi trước khi '
                            'phân biệt interval với tham số của row break, nên chưa tự sửa.')
                    evidence.append('Đầu đoạn bị đọc thành option: ' + segment[option.start():option.start() + 180])
                    confidence = 'scanner_path_confirmed_tex_semantics_unverified'
                    break
        if confidence == 'symptom_confirmed_root_cause_unverified':
            opener = re.search(r'\\begin\s*\{' + re.escape(env) + r'\}', snippet)
            if opener:
                raw = merged[location + opener.start():span[1]] if precision == 'exact_context' else segment
                display_end = raw.find(r'\]')
                named_end = re.search(r'\\end\s*\{' + re.escape(env) + r'\}', raw)
                if display_end >= 0 and (named_end is None or display_end < named_end.start()):
                    group = 'mixed_display_closure_requires_review'
                    note = (f'Source mở bằng \\begin{{{env}}} nhưng có \\] trước dấu đóng cùng tên. '
                            'Scanner hiện yêu cầu đóng cùng môi trường. Cần kiểm tra định nghĩa \\] '
                            'trong class/package trước khi hỗ trợ cách đóng này; chưa kết luận source hỏng.')
                    evidence.append('Dấu đóng khác dạng trong source: ' + raw[max(0, display_end - 70):display_end + 2])
                    confidence = 'syntax_observed_compile_behavior_unverified'
    if 'math' in error:
        for citation in cleanup.CITATION_RE.finditer(segment):
            j = citation.end()
            for _ in range(2):
                while j < len(segment) and segment[j].isspace(): j += 1
                option = cleanup.arg_at(segment, j, '[', ']')
                if option is None: break
                j = option[1]
            while j < len(segment) and segment[j].isspace(): j += 1
            key = cleanup.arg_at(segment, j)
            if key and '$' in key[0] and key[0] in snippet:
                group = 'dollar_in_citation_key'
                note = ('Citation key có ký tự dollar, nhưng scanner đi vào key và coi đó là dấu toán. '
                        'Cần giữ đối số citation như dữ liệu opaque; chưa sửa trong đợt bốn nhóm cú pháp này.')
                evidence.append('Citation key xác nhận: ' + key[0])
                confidence = 'confirmed_scanner_limitation'
                break
    return {'reason_group': group, 'note': note, 'evidence': ' '.join(evidence),
            'cause_confidence': confidence,
            'source_segment_span': span, 'source_line': line,
            'source_line_end': merged.count('\n', 0, span[1]) + 1,
            'location_precision': precision, 'source_excerpt': snippet}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        default=ROOT / 'src/cleanup_normalize/data/ok_input_cleanup_audit')
    parser.add_argument('--export-recovered', action='store_true',
                        help='Write recovered trees under OUTPUT/recovered without replacing the dataset.')
    args = parser.parse_args()
    summary = json.loads((ROOT / 'src/cleaned_structure/summary.json').read_text(encoding='utf-8'))
    selected = [r for r in summary['failed_records'] if r['input_status'] == 'ok']
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original_math, original_canonical = cleanup.math_token, cleanup.canonical_math
    original_consume = cleanup._Builder.consume
    context = {}

    def traced_math(text, offset):
        try:
            return original_math(text, offset)
        except ValueError:
            context.update(token_context=text[max(0, offset - 140):offset + 220],
                           segment_tail=text[-220:], offset=offset, segment_length=len(text))
            raise

    def traced_canonical(form, body):
        try:
            return original_canonical(form, body)
        except ValueError:
            context.update(math_form=form, body_start=body[:220])
            raise

    def traced_consume(builder, parent, text, origin):
        try:
            return original_consume(builder, parent, text, origin)
        except ValueError:
            context.setdefault('source_segment_span', list(origin))
            raise

    cleanup.math_token, cleanup.canonical_math = traced_math, traced_canonical
    cleanup._Builder.consume = traced_consume
    results, start = [], time.perf_counter()
    try:
        for i, failure in enumerate(selected, 1):
            context.clear()
            aid, version = failure['publication_key'], failure['version']
            folder = ROOT / 'src/parsed_structure/parsed_structure' / aid / f'v{version}'
            result = {'arxiv_id': aid.replace('-', '.'), 'version': version,
                      'previous_error': failure['reason'],
                      'source_path': str((folder / 'merged.tex').relative_to(ROOT)),
                      'outline_path': str((folder / 'outline.json').relative_to(ROOT))}
            merged = ''
            try:
                outline = json.loads((folder / 'outline.json').read_text(encoding='utf-8'))
                merged = (folder / 'merged.tex').read_text(encoding='utf-8')
                cleaned = cleanup.transform(merged, outline)
                result.update(result='recovered', current_error='', total_leaves=cleaned['total_leaves'],
                              cleanup_status=cleaned['cleanup_status'], diagnostics=cleaned.get('diagnostics', []))
                if args.export_recovered:
                    destination = output / 'recovered' / aid / f'v{version}' / 'cleaned_nodes.json'
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    cleanup.atomic_write(destination, json.dumps(cleaned, ensure_ascii=False, indent=2).encode('utf-8'))
                    result['recovered_output'] = str(destination.relative_to(output))
                del cleaned
            except (ValueError, KeyError, TypeError, OSError) as exc:
                result.update(result='still_failed', current_error=str(exc),
                              last_function=traceback.extract_tb(exc.__traceback__)[-1].name,
                              context=dict(context))
                result.update(failure_note(str(exc), context, merged))
            results.append(result)
            if i % 25 == 0 or i == len(selected):
                print(f'{i}/{len(selected)}: {dict(Counter(r["result"] for r in results))}', flush=True)
    finally:
        cleanup.math_token, cleanup.canonical_math = original_math, original_canonical
        cleanup._Builder.consume = original_consume
    remaining = [r for r in results if r['result'] == 'still_failed']
    report = {'previous_failed_ok_versions': len(selected),
              'publications': len({r['publication_key'] for r in selected}),
              'result_counts': dict(Counter(r['result'] for r in results)),
              'previous_errors': dict(Counter(r['previous_error'] for r in results)),
              'remaining_errors': dict(Counter(r['current_error'] for r in results if r['result'] == 'still_failed')),
              'remaining_publications': len({r['arxiv_id'] for r in remaining}),
              'remaining_groups': dict(Counter(r['reason_group'] for r in remaining)),
              'wall_seconds': round(time.perf_counter() - start, 2),
              'cleanup_code_sha256': hashlib.sha256(Path(cleanup.__file__).read_bytes()).hexdigest(),
              'scope': 'Only the 292 historically failed versions whose outline status is ok; upstream partial/failed excluded.',
              'note': 'Does not overwrite cleaned_structure trees or its summary. Recovered exports are isolated.',
              'versions': results}
    (output / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output / 'versions.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['arxiv_id', 'version', 'result', 'previous_error', 'current_error', 'total_leaves'], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)
    fields = ['arxiv_id', 'version', 'reason_group', 'current_error', 'note', 'evidence',
              'cause_confidence', 'source_path', 'source_line', 'source_line_end',
              'location_precision', 'source_segment_span', 'source_excerpt']
    with (output / 'unresolved_versions.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(remaining)
    papers = {}
    for row in remaining:
        paper = papers.setdefault(row['arxiv_id'], {'versions': [], 'groups': set()})
        paper['versions'].append('v' + str(row['version']))
        paper['groups'].add(row['reason_group'])
    with (output / 'unresolved_papers.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['arxiv_id', 'unresolved_versions', 'reason_groups'])
        for aid, paper in sorted(papers.items()):
            writer.writerow([aid, ';'.join(paper['versions']), ';'.join(sorted(paper['groups']))])
    lines = ['BÁO CÁO CLEANUP CÒN LỖI', '',
             f"Đã kiểm tra: {len(results)} phiên bản đầu vào ok từng bị cleanup lỗi.",
             f"Chạy được: {sum(r['result'] == 'recovered' for r in results)} phiên bản.",
             f"Còn lỗi: {len(remaining)} phiên bản / {len(papers)} paper.", '',
             'cause_confidence phân biệt nguyên nhân đã xác nhận và trường hợp còn cần kiểm tra.',
             'Không coi mọi lỗi scanner là lỗi LaTeX hoặc lỗi download.',
             'source_line có thể là đầu body_segment; xem location_precision trong CSV.',
             'Không tự sửa source hoặc bỏ đoạn lỗi để tăng số lượng thành công.', '',
             'unresolved_versions.csv: từng phiên bản, note, bằng chứng, source và dòng.',
             'unresolved_papers.csv: mỗi paper một dòng, các phiên bản và nhóm lỗi.',
             'audit.json: báo cáo chi tiết và context.',
             'recovered/: output đã chạy thành công nếu dùng --export-recovered.',
             'Dataset src/cleaned_structure và summary cũ chưa được thay thế.', '',]
    for group in sorted({r['reason_group'] for r in remaining}):
        rows = [r for r in remaining if r['reason_group'] == group]
        lines.extend([group, rows[0]['note']])
        for row in rows:
            lines.append(f"  {row['arxiv_id']}/v{row['version']} | {row['source_path']}:{row['source_line']} | {row['current_error']}")
            if row['evidence']: lines.append('    ' + row['evidence'])
        lines.append('')
    (output / 'README.txt').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'versions'}, indent=2))


if __name__ == '__main__':
    main()
