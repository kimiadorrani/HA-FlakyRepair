import re

with open('scripts/uiflaky/run_uiflaky_test.py', 'r') as f:
    code = f.read()

# Replace build_test_command
pattern1 = re.compile(r"def build_test_command\(.*?return \[\"docker\", \"exec\", container_name, \"bash\", \"-lc\", inner_cmd\]", re.DOTALL)

replacement1 = """def build_install_command(container_name: str) -> list[str]:
    # Force Yarn everywhere
    install_cmd = "yarn install --ignore-engines --non-interactive"
    return ["docker", "exec", container_name, "bash", "-lc", install_cmd]

def build_test_command(row: dict, repo_dir: Path, container_name: str) -> list[str]:
    test_files = row['Test Files'].split(';')
    test_files = [tf.strip() for tf in test_files if tf.strip()]
    test_files_str = " ".join(test_files)
    
    # Wrap in a shell
    inner_cmd = f'''
if grep -qi "jest" package.json; then
    npx jest {test_files_str}
elif grep -qi "mocha" package.json; then
    npx mocha {test_files_str}
elif grep -qi "cypress" package.json; then
    npx cypress run --spec {test_files_str}
elif grep -qi "karma" package.json; then
    npx karma start
else
    yarn test -- {test_files_str}
fi
'''
    return ["docker", "exec", container_name, "bash", "-lc", inner_cmd]"""

code = pattern1.sub(replacement1, code)

# Replace execute_row
pattern2 = re.compile(r"def execute_row\(.*?\n.*?try:\n.*?repo_dir = prepare_repo\(row, workspaces_dir\)\n.*?image = detect_node_image\(repo_dir\)\n.*?container_name = ensure_persistent_docker_container\(repo_dir, image\)\n.*?cmd = build_test_command\(row, repo_dir, container_name\)\n.*?print\(f\"Running UI-FLAKY test for \{row\['Project'\]\} :: \{row\['Title'\]\}\"\)\n.*?results_dir\.mkdir\(parents=True, exist_ok=True\)\n.*?return run_repeated\(cmd, repo_dir, iterations, row, results_dir\)", re.DOTALL)

replacement2 = """def execute_row(row_index: int, row: dict, workspaces_dir: Path, results_dir: Path, iterations: int):
    try:
        repo_dir = prepare_repo(row, workspaces_dir)
        image = detect_node_image(repo_dir)
        container_name = ensure_persistent_docker_container(repo_dir, image)
        
        print(f"[{row['Project']}] Running install...")
        install_cmd = build_install_command(container_name)
        subprocess.run(install_cmd, cwd=repo_dir, check=False)
        
        cmd = build_test_command(row, repo_dir, container_name)
        
        print(f"Running UI-FLAKY test for {row['Project']} :: {row['Title']}")
        results_dir.mkdir(parents=True, exist_ok=True)
        return run_repeated(cmd, repo_dir, iterations, row, results_dir)"""

code = pattern2.sub(replacement2, code)

with open('scripts/uiflaky/run_uiflaky_test.py', 'w') as f:
    f.write(code)

print("Updated script to separate install and test")
