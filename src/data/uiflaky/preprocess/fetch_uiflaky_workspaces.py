import csv
import subprocess
from pathlib import Path

def fetch_workspaces(metadata_path: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    with open(metadata_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        projects = set()
        for row in reader:
            projects.add((row['Project'], row['Project URL']))
            
    for project_name, project_url in sorted(projects):
        project_dir = target_dir / project_name.replace('/', '__')
        if not project_dir.exists():
            print(f'Cloning {project_name}...')
            subprocess.run(['git', 'clone', '--depth', '100', project_url, str(project_dir)])
        else:
            print(f'Project {project_name} already exists.')

if __name__ == '__main__':
    fetch_workspaces(Path('datasets/uiflaky/preprocessed/uiflaky-metadata.csv'), Path('workspaces/uiflaky'))
