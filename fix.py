with open('scripts/uiflaky/run_uiflaky_test.py', 'r') as f:
    code = f.read()

target = """    # Try to checkout, if fails, try to unshallow/deepen fetch
    result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Checkout {checkout} failed, attempting to deepen fetch...")
        subprocess.run(["git", "fetch", "--depth", "1000"], cwd=repo_dir, capture_output=True)
        result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Failed to checkout {checkout}, staying on current branch.")"""

replacement = """    # Try to checkout, if fails, try to unshallow/deepen fetch
    if commit_sha:
        subprocess.run(["git", "fetch", "origin", commit_sha], cwd=repo_dir, capture_output=True)

    result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Checkout {checkout} failed, attempting to deepen fetch...")
        subprocess.run(["git", "fetch", "--depth", "1000"], cwd=repo_dir, capture_output=True)
        result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Failed to checkout {checkout}, staying on current branch.")"""

if target in code:
    with open('scripts/uiflaky/run_uiflaky_test.py', 'w') as f:
        f.write(code.replace(target, replacement))
    print("Fixed git fetch logic")
else:
    print("Target not found!")
