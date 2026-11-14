<div align="center">

  <!-- BADGES (CĂN GIỮA & PHÓNG TO) -->
  <p align="center">
    <img src="https://img.shields.io/badge/HCMUS-FIT%20%7C%2024KHDL1-1b5e20?style=for-the-badge&logo=google-classroom&logoColor=white" alt="HCMUS 24KHDL1" height="34" />
    <img src="https://img.shields.io/badge/Course-Nh%E1%BA%ADp%20m%C3%B4n%20KHDL-0056D2?style=for-the-badge&logo=bookstack&logoColor=white" alt="Nhập môn KHDL" height="34" />
  </p>

  <h1>Project IntroData</h1>
  <p>Collect and processing scientific paper data from arXiv</p>
  <p>VNUHCM-University of Science · CLASS 24KHDL1</p>

</div>

## Team Members

| Full Name | Student ID
| --- | --- |
|Trương Phan Minh Hằng|24127033
|Nguyễn Trần Trọng Nhân|24127092
|Nguyễn Vĩnh Nguyên|24127470|
|Huỳnh Thị Yến Vy|24127594
---

This guide explains how to set up the environment and run the crawling and cleanup modules locally or on Google Colab.

## Environment Setup

Use Python 3.12 or a compatible newer version. Keep the complete `src` folder, including its subfolders, `requirements.txt`, and `entry_discovery/team_ranges.json`.

Create the environment from the project root, where `src` is located.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r src/requirements.txt
```

**Linux / macOS:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r src/requirements.txt
```

For Google Colab, use the runtime's Python environment and follow the setup cells described below.

## Crawling Section

The crawling pipeline discovers the assigned arXiv papers, downloads available LaTeX/BibTeX sources, and retrieves references from Semantic Scholar. Check the assigned ranges in `src/entry_discovery/team_ranges.json` before starting. An Internet connection is required.

### Run locally

If you have a Semantic Scholar API key, set it in the current terminal session. Skip this step to use public access.

**Windows PowerShell:**

```powershell
# Optional
$env:S2_API_KEY = "<YOUR_SEMANTIC_SCHOLAR_API_KEY>"

.\.venv\Scripts\python.exe src/run_m1.py
```

**Linux / macOS:**

```bash
# Optional
export S2_API_KEY="<YOUR_SEMANTIC_SCHOLAR_API_KEY>"

.venv/bin/python src/run_m1.py
```

The pipeline creates `data` beside `src` and stores its results under `data/output`. You do not need to create the input index or paper folders before running the complete pipeline.

### Run on Google Colab

1. Create a project folder in Google Drive, for example `MyDrive/IDS_Project`.
2. Upload the **complete `src` folder** into it. Keep all modules and `src/requirements.txt` together.
3. Open `src/pipeline_colab.ipynb` with Google Colab and choose a CPU runtime.
4. Run the notebook cells in order, replacing the Drive path with your own location.

Your Drive layout should look like this:

```text
MyDrive/
└── <YOUR_PROJECT_FOLDER>/
    ├── src/
    │   ├── pipeline_colab.ipynb
    │   ├── requirements.txt
    │   ├── run_m1.py
    │   └── ...
    └── data/                    # Created when the pipeline runs
```

**Mount Drive:**

```python
from google.colab import drive

drive.mount("/content/drive")
```

**Set the source directory:**

```python
%cd "/content/drive/MyDrive/<YOUR_PROJECT_FOLDER>/src"
```

Replace `<YOUR_PROJECT_FOLDER>` with the folder path inside your `MyDrive`. For example, `MyDrive/IDS_Project/src` becomes `/content/drive/MyDrive/IDS_Project/src`. Keep the quotes if your path contains spaces.

**Install dependencies in the current runtime:**

```python
%pip install -r requirements.txt
```

**Load an optional API key:**

Open the Colab **Secrets** panel using the key icon. Add a secret named `S2_API_KEY`, set its value, and enable **Notebook access**. Then run:

```python
import os
from google.colab import userdata

try:
    api_key = userdata.get("S2_API_KEY")
except (userdata.SecretNotFoundError, userdata.NotebookAccessError):
    api_key = None

if api_key:
    os.environ["S2_API_KEY"] = api_key
    print("API key loaded from Colab Secrets.")
else:
    print("No accessible Colab API key; using environment settings or public access.")
```

The cell does not display the key. If no accessible secret is available, the crawler uses its existing environment settings or public access.

**Start crawling:**

```python
%run run_m1.py
```

Run the dependency and setup cells again whenever Colab starts a new runtime. Results remain in the project folder on Drive.

## Cleanup and Normalization Section

In progress...

## Troubleshooting

- **Missing Python packages:** install `src/requirements.txt` with the interpreter used to run the pipeline. In Colab, run the `%pip` cell after switching to `src`.
- **`requirements.txt` not found:** check the path in the `%cd` cell. The working directory must contain the uploaded source files.
- **Colab still uses old code:** stop the current run, replace the files in the correct Drive folder, restart the runtime, and run the setup cells again.
- **Secret not found or inaccessible:** check the exact name `S2_API_KEY` and enable notebook access. An API key does not guarantee that requests will avoid rate limiting.
- **Missing cleanup inputs:** prepare or upload the matching `merged.tex` and `outline.json` files before running cleanup.

Keep real API keys, virtual environments, downloaded data, and notebook execution outputs out of Git.
