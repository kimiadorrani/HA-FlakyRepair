with open('scripts/uiflaky/run_uiflaky_test.py', 'r') as f:
    code = f.read()

target = """    result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        # try fetch
        print(f"Checkout {checkout} failed, attempting to deepen fetch...")
        subprocess.run(["git", "fetch", "--unshallow"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "fetch", "origin", checkout], cwd=repo_dir, capture_output=True)
        result2 = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
        if result2.returncode != 0:
            print(f"Warning: Failed to checkout {checkout}, staying on current branch.")"""

replacement = """    # Always fetch the base commit first
    base_commit = row['Commit SHA']
    subprocess.run(["git", "fetch", "origin", base_commit], cwd=repo_dir, capture_output=True)
    
    result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Checkout {checkout} failed, attempting to deepen fetch...")
        subprocess.run(["git", "fetch", "--unshallow"], cwd=repo_dir, capture_output=True)
        result2 = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
        if result2.returncode != 0:
            print(f"Warning: Failed to checkout {checkout}, staying on current branch.")"""

if target in code:
    code = code.replace(target, replacement)
    with open('scripts/uiflaky/run_uiflaky_test.py', 'w') as f:
        f.write(code)
    print("Replaced successfully")
else:
    print("Target not found")
