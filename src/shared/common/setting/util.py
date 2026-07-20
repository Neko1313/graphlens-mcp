from pathlib import Path


def create_dir(app_path: str) -> Path:
    path = Path(app_path)
    if not path.exists():
        path.mkdir(parents=True)
    return path
