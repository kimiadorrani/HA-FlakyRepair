import re

with open('scripts/uiflaky/run_uiflaky_test.py', 'r') as f:
    code = f.read()

# 1. Simplify setup_cmd to just install yarn
pattern1 = re.compile(r'setup_cmd = \([\s\S]*?\n        \)')
replacement1 = '''setup_cmd = (
            "apt-get update && apt-get install -y python2 python3 python-is-python3 make g++ libpng-dev libjpeg-dev libgif-dev curl && "
            "npm install -g yarn || true"
        )'''
code = pattern1.sub(replacement1, code)

# 2. Force yarn in build_test_command
pattern2 = re.compile(r'def build_test_command\([\s\S]*?return \["docker", "exec", container_name, "bash", "-lc", inner_cmd\]')

replacement2 = """def build_test_command(row: dict, repo_dir: Path, container_name: str) -> list[str]:
    test_files = row['Test Files'].split(';')
    # Clean whitespace and filter empty
    test_files = [tf.strip() for tf in test_files if tf.strip()]
    test_files_str = " ".join(test_files)
    
    # Force Yarn everywhere as requested
    install_cmd = "yarn install --ignore-engines"

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
    yarn test -- {test_files_str}
fi
'''
    return ["docker", "exec", container_name, "bash", "-lc", inner_cmd]"""

code = pattern2.sub(replacement2, code)

with open('scripts/uiflaky/run_uiflaky_test.py', 'w') as f:
    f.write(code)

print("Updated script to force Yarn globally")
