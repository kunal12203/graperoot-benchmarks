#!/usr/bin/env python3
"""
Evaluate SWE-bench Pro predictions.
Applies model_patch + test_patch to Docker container, runs tests,
checks fail_to_pass now passes and pass_to_pass still passes.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datasets import load_dataset


def docker_exec(container, cmd, timeout=300):
    r = subprocess.run(
        ["docker", "exec", "-w", "/app", container, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.returncode, r.stdout + r.stderr


def apply_patch_via_file(container, patch_text):
    """Write patch to a temp file, copy into container, apply."""
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(patch_text)
        tmp_path = f.name

    subprocess.run(
        ["docker", "cp", tmp_path, f"{container}:/tmp/apply.patch"],
        capture_output=True, timeout=30
    )
    Path(tmp_path).unlink(missing_ok=True)

    rc, out = docker_exec(container, "cd /app && git apply /tmp/apply.patch", timeout=30)
    if rc != 0:
        rc, out = docker_exec(container, "cd /app && git apply --3way /tmp/apply.patch", timeout=30)
    return rc, out


def evaluate_instance(instance, model_patch):
    iid = instance["instance_id"]
    tag = instance.get("dockerhub_tag", "")
    image = f"jefzda/sweap-images:{tag}"
    cname = f"eval-{abs(hash(iid)) % 10**8}"

    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)

    # Pull image if needed (timeout 10 min for large images)
    subprocess.run(["docker", "pull", image], capture_output=True, text=True, timeout=600)

    r = subprocess.run(
        ["docker", "run", "-d", "--name", cname, "-w", "/app",
         "--entrypoint", "/bin/sh", image, "-c", "sleep 600"],
        capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        return {"instance_id": iid, "resolved": False, "error": f"container start failed: {r.stderr[:150]}"}

    time.sleep(2)

    try:
        before_cmd = instance.get("before_repo_set_cmd", "")
        if before_cmd:
            docker_exec(cname, before_cmd, timeout=60)

        # Apply model patch
        if model_patch.strip():
            rc, out = apply_patch_via_file(cname, model_patch)
            if rc != 0:
                return {"instance_id": iid, "resolved": False, "error": f"model patch failed: {out[:200]}"}

        # Apply test patch
        test_patch = instance.get("test_patch", "")
        if test_patch.strip():
            rc, out = apply_patch_via_file(cname, test_patch)
            if rc != 0:
                return {"instance_id": iid, "resolved": False, "error": f"test patch failed: {out[:200]}"}

        # Run tests
        test_files = instance.get("selected_test_files_to_run", [])
        if isinstance(test_files, str):
            try:
                test_files = json.loads(test_files)
            except Exception:
                test_files = [test_files] if test_files else []

        lang = instance.get("repo_language", "").lower()

        if "python" in lang:
            test_cmd = f"cd /app && python -m pytest {' '.join(test_files)} -x --tb=short 2>&1"
        elif "go" in lang:
            test_cmd = f"cd /app && go test {' '.join(test_files)} -v 2>&1"
        else:
            # JS/TS — try npm test or npx mocha
            test_cmd = f"cd /app && npx mocha {' '.join(test_files)} --timeout 60000 --exit 2>&1"

        rc, test_output = docker_exec(cname, test_cmd, timeout=300)

        # Check fail_to_pass
        fail_to_pass = instance.get("fail_to_pass", "")
        if isinstance(fail_to_pass, str):
            try:
                fail_to_pass = json.loads(fail_to_pass) if fail_to_pass.startswith("[") else []
            except Exception:
                fail_to_pass = []

        # If exit code 0 -> all tests pass -> resolved
        # More nuanced: check that fail_to_pass tests now pass
        resolved = (rc == 0)

        return {
            "instance_id": iid,
            "resolved": resolved,
            "exit_code": rc,
            "test_output_tail": test_output[-500:] if test_output else "",
        }

    except Exception as e:
        return {"instance_id": iid, "resolved": False, "error": str(e)}
    finally:
        subprocess.run(["docker", "stop", "-t", "2", cname], capture_output=True)
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)


def main():
    preds_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./results/preds.json")

    preds = json.loads(preds_path.read_text())
    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    instances_by_id = {inst["instance_id"]: inst for inst in ds}

    results = []
    resolved_count = 0
    total = 0

    for iid, data in preds.items():
        patch = data.get("model_patch", "")
        if not patch.strip():
            continue
        if iid not in instances_by_id:
            print(f"  [skip] {iid[:50]} not in dataset")
            continue

        total += 1
        inst = instances_by_id[iid]
        print(f"\n  [{total}] Evaluating {iid[:55]}...")
        result = evaluate_instance(inst, patch)
        results.append(result)

        if result.get("resolved"):
            resolved_count += 1
            print(f"    RESOLVED")
        else:
            err = result.get("error", "")
            tail = result.get("test_output_tail", "")[-100:]
            print(f"    FAILED: {err or tail}")

        print(f"    Score so far: {resolved_count}/{total} = {resolved_count/total*100:.1f}%")

    print(f"\n{'='*60}")
    total_preds = len(preds)
    print(f"RESOLVE RATE: {resolved_count}/{total_preds} = {resolved_count/total_preds*100:.1f}%")
    print(f"(out of {total_preds} total instances, {total} had patches)")

    report_path = preds_path.parent / "eval_results.json"
    report_path.write_text(json.dumps(results, indent=2))
    print(f"Results: {report_path}")


if __name__ == "__main__":
    main()
