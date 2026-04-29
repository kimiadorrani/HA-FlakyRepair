with open('scripts/uiflaky/run_uiflaky_test.py', 'r') as f:
    code = f.read()

import re

pattern = re.compile(r"def build_test_command\(row: dict, repo_dir: Path, container_name: str\) -> list\[str\]:[\s\S]*?return \[\"docker\", \"exec\", container_name, \"bash\", \"-lc\", inner_cmd\]")

replacement = """def build_test_command(row: dict, repo_dir: Path, container_name: str) -> list[str]:
    test_files = row['Test Files'].split(';')
    # Clean whitespace and filter empty
    test_files = [tf.strip() for tf in test_files if tf.strip()]
    test_files_str = " ".join(test_files)
    
    # Detect package manager
    if (repo_dir / "pnpm-lock.yaml").exists():
        tool = "pnpm"
        install_cmd = "pnpm install"
    elif (repo_dir / "yarn.lock").exists():
        tool = "yarn"
        install_cmd = "yarn install --ignore-engines"
    else:
        tool = "npm"
        install_cmd = "npm install --unsafe-perm --no-audit --no-fund --legacy-peer-deps || npm install --unsafe-perm --no-audit --no-fund"

    # Wrap in a shell
    inner_cmd = f'''
{install_cmd}
if grep -qi "jest" package.json; then
    npx jest {test_files_str}
elif grep -qi "mocha" package.json; then
    npx mocha {test_files_str}
elif grep -qi "cypress" package.json; then
    npx cypress run --spec {test_files_str}
elif grep -qi "karma" package.json; then
    npx karma start
else
    {tool} test -- {test_files_str}
fi
'''
    return ["docker", "exec", container_name, "bash", "-lc", inner_cmd]"""

if pattern.search(code):
    code = pattern.sub(replacement, code)
    with open('scripts/uiflaky/run_uiflaky_test.py', 'w') as f:
        f.write(code)
    print("Fixed build_test_command")
else:
    print("Pattern not found")
