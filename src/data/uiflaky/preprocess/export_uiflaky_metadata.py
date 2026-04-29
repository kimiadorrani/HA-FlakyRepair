import csv
import re
from pathlib import Path

def parse_uiflaky_csv(input_path: Path):
    rows = []
    with open(input_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row.get('Title', '')
            url = row.get('URL', '')
            tests_affected = row.get('Tests/Code Affected (to Reproduce)', '')
            
            if not url or url == 'Web' or url == 'URL':
                continue
                
            project_match = re.search(r'github\.com/([^/]+/[^/]+)/commit/([0-9a-f]+)', url)
            if not project_match:
                project_match = re.search(r'github\.com/([^/]+/[^/]+)', url)
                commit = ''
            else:
                commit = project_match.group(2)
            
            if not project_match:
                continue
                
            project_full = project_match.group(1)
            
            # Extract Test Files and filter for JS/TS
            test_files = []
            test_urls = re.findall(r'https?://github\.com/[^/]+/[^/]+/blob/([0-9a-f]+)/(\S+)', tests_affected)
            for sha, path in test_urls:
                if any(path.endswith(ext) for ext in ['.js', '.ts', '.jsx', '.tsx', '.coffee']):
                    test_files.append(path)
                    if not commit: commit = sha
            
            if not test_files:
                potential_files = re.findall(r'\S+\.(?:js|ts|jsx|tsx|coffee)', tests_affected)
                test_files = potential_files
            
            if not test_files:
                continue
                
            rows.append({
                'Dataset': 'UI-FLAKY',
                'Project': project_full,
                'Project URL': f'https://github.com/{project_full}',
                'Commit SHA': commit,
                'Test Files': ';'.join(test_files),
                'Category': row.get('Root Cause Category', 'Unspecified'),
                'Title': title.strip(),
                'Source URL': url
            })
    return rows

if __name__ == '__main__':
    input_csv = Path('datasets/uiflaky/raw/repo/dataset.csv')
    output_csv = Path('datasets/uiflaky/preprocessed/uiflaky-metadata.csv')
    
    metadata = parse_uiflaky_csv(input_csv)
    
    if metadata:
        with open(output_csv, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=metadata[0].keys())
            writer.writeheader()
            writer.writerows(metadata)
        print(f'Exported {len(metadata)} JS/TS rows to {output_csv}')
