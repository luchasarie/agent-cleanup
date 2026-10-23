"""End-to-end tests: build real git fixtures, run scan and apply as a user would.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "cleanup" / "scripts" / "agent_cleanup.py"
OLD = time.time() - 60 * 86400


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-cleanup-test-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.tmp / "state"),
                        AGENT_CLEANUP_NO_GH="1", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.env.pop("CODEX_HOME", None)
        self.dev = self.tmp / "dev"
        self.dev.mkdir()
        self.origin = self.tmp / "origin.git"
        self.sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.dev / "app"
        self.sh("git", "clone", "-q", str(self.origin), str(self.repo))
        self.commit(self.repo, "README.md", "hello\n")
        self.git("push", "-q", "-u", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args, cwd=None, env=None):
        return subprocess.run(args, cwd=cwd, env=env or self.env, check=True,
                              capture_output=True, text=True).stdout

    def git(self, *args, cwd=None, env=None):
        return self.sh("git", "-C", str(cwd or self.repo), *args, env=env)

    def commit(self, where, name, text, old=False):
        (Path(where) / name).write_text(text)
        env = dict(self.env)
        if old:
            stamp = f"@{int(OLD)} +0000"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        self.git("add", name, cwd=where)
        self.git("commit", "-q", "-m", f"add {name}", cwd=where, env=env)

    def cli(self, *args, check=True):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.tmp, env=self.env,
                           capture_output=True, text=True)
        if check and p.returncode not in (0, 1):
            self.fail(f"exit {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def call(self, expr, *args):
        """Evaluate expr with the script imported as `a` (sys.argv[1:] = args); returns its JSON."""
        code = ("import json, sys; sys.path.insert(0, sys.argv.pop(1)); import agent_cleanup as a\n"
                f"print(json.dumps({expr}))")
        return json.loads(self.sh(sys.executable, "-c", code, str(SCRIPT.parent), *args, cwd=self.tmp))

    def scan(self, *extra):
        out = self.cli("scan", "--json", "--min-size", "0", "--only", "git,artifacts,archives",
                       "--root", str(self.dev), *extra).stdout
        return json.loads(out)["items"]

    def find(self, items, kind, needle):
        hits = [i for i in items if i["kind"] == kind
                and (needle in i["path"] or needle == i.get("branch"))]
        self.assertEqual(len(hits), 1, f"{kind} {needle}: {hits}")
        return hits[0]

    def worktree(self, name, branch):
        path = self.dev / name
        self.git("worktree", "add", "-q", "-b", branch, str(path))
        return path

    def age(self, path):
        """Make a checkout look untouched for 60 days."""
        idx = self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=path).strip()
        for p in [Path(idx), *Path(path).rglob("*"), Path(path)]:
            os.utime(p, (OLD, OLD), follow_symlinks=False)


class ScanTests(Fixture):
    def test_squash_merged_worktree_is_remove(self):
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n")
        self.git("merge", "--squash", "feat")
        self.git("commit", "-q", "-m", "squash feat")
        self.git("push", "-q", "origin", "main")
        self.age(wt)
        it = self.find(self.scan(), "worktree", "app-feat")
        self.assertEqual(it["class"], "REMOVE")
        self.assertIn("content already on origin/main", it["note"])
        self.assertIn("1 local-only commit(s) saved", it["note"])

    def test_merged_worktree_edited_recently_is_kept(self):
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n")
        self.git("merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.git("push", "-q", "origin", "main")
        it = self.find(self.scan(), "worktree", "app-feat")
        self.assertEqual(it["class"], "KEEP-ACTIVE")
        self.assertIn("files touched 0h ago", it["note"])
        self.assertEqual(self.find(self.scan("--recent-hours", "0"), "worktree", "app-feat")["class"], "REMOVE")

    def test_husky_hook_shims_are_disposable(self):
        (self.repo / ".gitignore").write_text(".husky/_/\n")
        (self.repo / ".husky").mkdir()
        (self.repo / ".husky" / "pre-commit").write_text("npm test\n")
        self.git("add", ".gitignore", ".husky/pre-commit")
        self.git("commit", "-q", "-m", "husky")
        self.git("push", "-q", "origin", "main")
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n")
        self.git("merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.git("push", "-q", "origin", "main")
        (wt / ".husky" / "_").mkdir()
        (wt / ".husky" / "_" / "pre-commit").write_text("#!/bin/sh\n")
        self.age(wt)
        self.assertEqual(self.find(self.scan(), "worktree", "app-feat")["class"], "REMOVE")

    def test_entries_inside_husky_shims_are_disposable(self):
        names = [".husky/_", ".husky/_/husky.sh", "web/.husky/_/pre-commit", ".husky/pre-commit", "x.husky/_/h"]
        self.assertEqual(self.call(f"[a.disposable(n) for n in {names!r}]"), [True, True, True, False, False])

    def test_merged_worktree_too_big_to_walk_needs_decision(self):
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n")
        self.git("merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.git("push", "-q", "origin", "main")
        self.age(wt)
        self.env["AGENT_CLEANUP_TREE_LIMIT"] = "1"
        it = self.find(self.scan(), "worktree", "app-feat")
        self.assertEqual(it["class"], "NEEDS-DECISION")
        self.assertIn("merged into origin/main, but too many files to confirm", it["note"])

    def test_pr_merged_branch_notes_local_only_commits(self):
        self.git("branch", "pr-branch")
        self.git("switch", "-q", "pr-branch")
        self.commit(self.repo, "p.txt", "only local\n", old=True)
        tip = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "main")
        fake = self.tmp / "bin"
        fake.mkdir()
        pr = [{"headRefName": "pr-branch", "state": "MERGED", "number": 7, "headRefOid": tip,
               "baseRefName": "main"}]
        (fake / "gh").write_text(f"#!/bin/sh\necho '{json.dumps(pr)}'\n")
        (fake / "gh").chmod(0o755)
        self.env.pop("AGENT_CLEANUP_NO_GH")
        self.env["PATH"] = f"{fake}{os.pathsep}{self.env['PATH']}"
        it = self.find(self.scan(), "branch", "pr-branch")
        self.assertEqual(it["class"], "REMOVE")
        self.assertEqual(it["note"], "PR #7 merged; 1 local-only commit(s) saved under refs/agent-cleanup/ on delete")

    def test_dirty_worktree_needs_decision(self):
        wt = self.worktree("app-dirty", "dirty")
        (wt / "scratch.txt").write_text("wip\n")
        it = self.find(self.scan(), "worktree", "app-dirty")
        self.assertEqual(it["class"], "NEEDS-DECISION")
        self.assertIn("uncommitted", it["note"])

    def test_missing_worktree_is_remove(self):
        wt = self.worktree("app-gone", "gone")
        shutil.rmtree(wt)
        it = self.find(self.scan(), "worktree", "app-gone")
        self.assertEqual(it["class"], "REMOVE")

    def test_fresh_branch_kept_and_stale_unmerged_needs_decision(self):
        self.git("branch", "fresh")
        self.git("switch", "-q", "-c", "old")
        self.commit(self.repo, "old.txt", "old\n", old=True)
        self.git("switch", "-q", "main")
        items = self.scan()
        self.assertEqual(self.find(items, "branch", "fresh")["class"], "KEEP-ACTIVE")
        old = self.find(items, "branch", "old")
        self.assertEqual(old["class"], "NEEDS-DECISION")
        self.assertIn("stale", old["note"])

    def test_main_checkout_skipped(self):
        it = self.find(self.scan(), "worktree", str(self.repo))
        self.assertEqual(it["class"], "SKIP")

    def test_orphan_worktree(self):
        orphan = self.dev / "orphan"
        orphan.mkdir()
        (orphan / ".git").write_text(f"gitdir: {self.tmp}/deleted/.git/worktrees/orphan\n")
        it = self.find(self.scan(), "orphan-worktree", "orphan")
        self.assertEqual(it["class"], "NEEDS-DECISION")

    def test_artifacts_in_stale_project_only_when_ignored(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        self.commit(self.repo, ".gitignore", "node_modules/\n", old=True)
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        (self.repo / "node_modules" / "x" / "i.js").write_text("1")
        (self.repo / "dist").mkdir()  # not ignored: must not be offered
        self.git("push", "-q", "origin", "main")
        self.age(self.repo)
        it = self.find(self.scan(), "artifacts", str(self.repo))
        self.assertEqual([Path(p).name for p in it["paths"]], ["node_modules"])

    def test_old_archive_and_backup_dir(self):
        (self.dev / "dump.tar.gz").write_bytes(b"x" * 10)
        backup = self.dev / "app.bak-20260101"
        backup.mkdir()
        (backup / "f").write_text("x")
        for p in (self.dev / "dump.tar.gz", backup / "f", backup):
            os.utime(p, (OLD, OLD))
        items = self.scan()
        self.assertEqual(self.find(items, "archive", "dump.tar.gz")["class"], "NEEDS-DECISION")
        self.assertEqual(self.find(items, "backup", "app.bak-20260101")["class"], "NEEDS-DECISION")


    def test_ignored_unique_file_blocks_remove(self):
        (self.repo / ".gitignore").write_text(".env\nnode_modules/\n")
        self.commit(self.repo, ".gitignore", ".env\nnode_modules/\n")
        self.git("push", "-q", "origin", "main")
        wt = self.worktree("app-env", "env")
        self.commit(wt, "f.txt", "x\n")
        self.git("merge", "-q", "--no-ff", "env", "-m", "merge env")
        self.git("push", "-q", "origin", "main")
        (wt / "node_modules").mkdir()
        self.age(wt)
        it = self.find(self.scan(), "worktree", "app-env")
        self.assertEqual(it["class"], "REMOVE")  # node_modules alone is disposable
        (wt / ".env").write_text("SECRET=1\n")
        it = self.find(self.scan(), "worktree", "app-env")
        self.assertEqual(it["class"], "NEEDS-DECISION")
        self.assertIn(".env", it["note"])

    def test_long_lived_branch_skipped(self):
        self.git("branch", "develop")
        self.assertEqual(self.find(self.scan(), "branch", "develop")["class"], "SKIP")

    def test_new_branch_at_old_base_is_kept(self):
        self.git("switch", "-q", "-c", "base-old")  # make main's tip old
        self.git("switch", "-q", "main")
        env = dict(self.env, GIT_COMMITTER_DATE=f"@{int(OLD)} +0000", GIT_AUTHOR_DATE=f"@{int(OLD)} +0000")
        self.git("commit", "-q", "--allow-empty", "-m", "old tip", env=env)
        self.git("push", "-q", "origin", "main")
        self.git("branch", "brand-new")
        it = self.find(self.scan(), "branch", "brand-new")
        self.assertEqual(it["class"], "KEEP-ACTIVE")

    def test_rebase_in_progress_protects_branch(self):
        wt = self.worktree("app-rebase", "rb")
        self.commit(wt, "README.md", "theirs\n")
        self.commit(self.repo, "README.md", "ours\n")
        subprocess.run(["git", "-C", str(wt), "rebase", "main"], env=self.env, capture_output=True)
        items = self.scan()
        self.assertEqual(self.find(items, "worktree", "app-rebase")["class"], "SKIP")
        self.assertFalse([i for i in items if i["kind"] == "branch" and i.get("branch") == "rb"])


class ApplyTests(Fixture):
    def merged_worktree(self):
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n")
        self.git("merge", "-q", "--no-ff", "feat", "-m", "merge feat")
        self.git("push", "-q", "origin", "main")
        self.age(wt)
        return wt

    def test_recommended_removes_merged_and_leaves_the_rest(self):
        wt = self.merged_worktree()
        dirty = self.worktree("app-dirty", "dirty")
        (dirty / "wip.txt").write_text("wip\n")
        self.scan()
        out = self.cli("apply", "--recommended").stdout
        self.assertIn("worktree removed, branch feat deleted", out)
        self.assertFalse(wt.exists())
        self.assertNotIn("feat", self.git("branch", "--list", "feat"))
        self.assertTrue((dirty / "wip.txt").exists())

    def test_dry_run_changes_nothing(self):
        wt = self.merged_worktree()
        self.scan()
        out = self.cli("apply", "--recommended", "--dry-run").stdout
        self.assertIn("would git worktree remove", out)
        self.assertTrue(wt.exists())

    def test_item_changed_since_scan_is_skipped(self):
        wt = self.merged_worktree()
        item = self.find(self.scan(), "worktree", "app-feat")
        (wt / "new.txt").write_text("started more work\n")
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("skipped: changed since scan", out)
        self.assertTrue((wt / "new.txt").exists())

    def test_needs_decision_worktree_never_forced_by_default(self):
        dirty = self.worktree("app-dirty", "dirty")
        (dirty / "wip.txt").write_text("wip\n")
        item = self.find(self.scan(), "worktree", "app-dirty")
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("git refused", out)
        self.assertTrue((dirty / "wip.txt").exists())

    def test_keep_active_is_refused(self):
        self.git("branch", "fresh")
        item = self.find(self.scan(), "branch", "fresh")
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("refused", out)
        self.assertIn("fresh", self.git("branch", "--list", "fresh"))

    def test_missing_worktree_keeps_unmerged_branch(self):
        wt = self.worktree("app-gone", "unique")
        self.commit(wt, "u.txt", "only here\n")
        shutil.rmtree(wt)
        items = self.scan()
        gone = self.find(items, "worktree", "app-gone")
        self.assertEqual(gone["class"], "REMOVE")
        self.assertEqual(self.find(items, "branch", "unique")["class"], "KEEP-ACTIVE")
        out = self.cli("apply", "--recommended").stdout
        self.assertIn("unregistered; branch unique kept", out)
        self.assertIn("unique", self.git("branch", "--list", "unique"))

    def test_force_removes_dirty_worktree_when_asked(self):
        dirty = self.worktree("app-dirty", "dirty")
        (dirty / "wip.txt").write_text("wip\n")
        item = self.find(self.scan(), "worktree", "app-dirty")
        out = self.cli("apply", "--force", str(item["id"])).stdout
        self.assertIn("worktree removed; branch dirty kept", out)
        self.assertFalse(dirty.exists())

    def test_detached_unique_commit_is_rescued(self):
        path = self.dev / "app-detached"
        self.git("worktree", "add", "-q", "--detach", str(path))
        self.commit(path, "d.txt", "detached work\n", old=True)
        self.age(path)
        head = self.git("rev-parse", "HEAD", cwd=path).strip()
        item = self.find(self.scan(), "worktree", "app-detached")
        self.assertEqual(item["class"], "NEEDS-DECISION")
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("detached HEAD saved as refs/agent-cleanup/", out)
        self.assertIn(head, self.git("for-each-ref", "refs/agent-cleanup/"))

    def test_squash_merged_branch_with_local_only_commits_is_rescued(self):
        self.git("checkout", "-q", "-b", "local")
        self.commit(self.repo, "l.txt", "local only\n", old=True)
        tip = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        self.git("merge", "--squash", "local")
        self.git("commit", "-q", "-m", "squash local")
        self.git("push", "-q", "origin", "main")
        item = self.find(self.scan(), "branch", "local")
        self.assertEqual(item["class"], "REMOVE")
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("local-only commits saved as refs/agent-cleanup/branches/local-", out)
        self.assertEqual(self.git("branch", "--list", "local").strip(), "")
        self.assertIn(tip, self.git("for-each-ref", "refs/agent-cleanup/"))

    def squash_merged_worktree(self):
        wt = self.worktree("app-feat", "feat")
        self.commit(wt, "feat.txt", "feature\n", old=True)
        self.git("merge", "--squash", "feat")
        self.git("commit", "-q", "-m", "squash feat")
        self.git("push", "-q", "origin", "main")
        self.age(wt)
        item = self.find(self.scan(), "worktree", "app-feat")
        self.assertEqual(item["class"], "REMOVE")
        return wt, item

    def test_failed_branch_rescue_removes_nothing(self):
        wt, item = self.squash_merged_worktree()
        self.git("update-ref", "refs/agent-cleanup/branches", "HEAD")  # blocks refs under it
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("failed: could not save feat as refs/agent-cleanup/branches/feat-", out)
        self.assertIn("nothing deleted", out)
        self.assertTrue(wt.exists())
        self.assertIn("feat", self.git("branch", "--list", "feat"))

    def test_unresolvable_branch_is_never_treated_as_pushed(self):
        with self.assertRaises(subprocess.CalledProcessError) as e:
            self.call("a.rescue_branch(sys.argv[1], 'no-such-branch')", str(self.repo))
        self.assertIn("could not resolve branch no-such-branch", e.exception.stderr)

    def test_failed_branch_delete_still_reports_rescue_ref(self):
        wt, item = self.squash_merged_worktree()
        lock = Path(self.git("rev-parse", "--git-path", "refs/heads/feat.lock").strip())
        lock = lock if lock.is_absolute() else self.repo / lock
        lock.write_text("")  # makes `git branch -D feat` fail
        out = self.cli("apply", str(item["id"])).stdout
        self.assertIn("worktree removed; branch feat kept", out)
        self.assertIn("local-only commits saved as refs/agent-cleanup/branches/feat-", out)
        self.assertFalse(wt.exists())

        self.git("worktree", "add", "-q", "-b", "local", str(self.dev / "tmp-wt"))
        self.commit(self.dev / "tmp-wt", "l.txt", "local only\n", old=True)
        self.git("worktree", "remove", str(self.dev / "tmp-wt"))
        self.git("merge", "--squash", "local")
        self.git("commit", "-q", "-m", "squash local")
        self.git("push", "-q", "origin", "main")
        branch = self.find(self.scan(), "branch", "local")
        self.assertEqual(branch["class"], "REMOVE")
        (lock.parent / "local.lock").write_text("")
        out = self.cli("apply", str(branch["id"])).stdout
        self.assertIn("failed: git refused", out)
        self.assertIn("local-only commits saved as refs/agent-cleanup/branches/local-", out)

    def test_plan_without_recent_hours_still_applies(self):
        wt = self.merged_worktree()
        self.scan()
        plan_file = self.tmp / "state" / "agent-cleanup" / "plan.json"
        plan = json.loads(plan_file.read_text())
        del plan["options"]["recent_hours"]
        plan_file.write_text(json.dumps(plan))
        out = self.cli("apply", "--recommended").stdout
        self.assertIn("worktree removed, branch feat deleted", out)
        self.assertFalse(wt.exists())

    def test_remote_branch_needs_flag_and_lease(self):
        wt = self.merged_worktree()
        self.git("push", "-q", "origin", "feat")
        items = self.scan("--remote")
        remote = self.find(items, "remote-branch", "feat")
        out = self.cli("apply", str(remote["id"])).stdout
        self.assertIn("needs --remote", out)
        # someone pushes to the branch after the scan: the lease must refuse
        other = self.tmp / "other"
        self.sh("git", "clone", "-q", "-b", "feat", str(self.origin), str(other))
        self.commit(other, "late.txt", "late\n")
        self.git("push", "-q", "origin", "feat", cwd=other)
        out = self.cli("apply", "--remote", str(remote["id"])).stdout
        self.assertIn("push refused", out)
        self.assertIn("feat", self.sh("git", "ls-remote", "--heads", str(self.origin), "feat"))

    def test_remote_branch_deleted_when_unchanged(self):
        self.merged_worktree()
        self.git("push", "-q", "origin", "feat")
        remote = self.find(self.scan("--remote"), "remote-branch", "feat")
        out = self.cli("apply", "--remote", str(remote["id"])).stdout
        self.assertIn("origin/feat deleted", out)
        self.assertEqual("", self.sh("git", "ls-remote", "--heads", str(self.origin), "feat"))

    def test_trash_changed_after_scan_is_skipped(self):
        files = self.home / ".local/share/Trash/files"
        files.mkdir(parents=True)
        (files / "old.txt").write_text("x")
        out = self.cli("scan", "--json", "--min-size", "0", "--only", "trash").stdout
        item = json.loads(out)["items"][0]
        (files / "needed.txt").write_text("trashed after the scan")
        self.assertIn("changed since scan", self.cli("apply", str(item["id"])).stdout)
        self.assertTrue((files / "needed.txt").exists())

    def test_agent_data_spares_claude_memory(self):
        proj = self.home / ".claude/projects/-x"
        (proj / "memory").mkdir(parents=True)
        for f in (proj / "s.jsonl", proj / "memory" / "MEMORY.md"):
            f.write_text("x")
            os.utime(f, (OLD, OLD))
        out = self.cli("scan", "--json", "--min-size", "0", "--only", "agents").stdout
        item = json.loads(out)["items"][0]
        self.cli("apply", str(item["id"]))
        self.assertFalse((proj / "s.jsonl").exists())
        self.assertTrue((proj / "memory" / "MEMORY.md").exists())

    def test_build_output_needs_decision(self):
        proj = self.dev / "old-lib"
        for d in ("node_modules", "dist"):
            (proj / d).mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        for p in (proj / "node_modules", proj / "dist", proj / "package.json", proj):
            os.utime(p, (OLD, OLD))
        items = self.scan()
        self.assertFalse([i for i in items if "old-lib" in i["path"] and i["kind"] == "build-output"])
        # dist outside git isn't unambiguous, so it is never offered; node_modules is
        self.assertEqual(self.find(items, "artifacts", "old-lib")["class"], "REMOVE")

    def test_bad_ids_rejected(self):
        self.scan()
        p = self.cli("apply", "5-2", check=False)
        self.assertEqual(p.returncode, 2)

    def test_artifacts_deleted(self):
        proj = self.dev / "old-site"
        (proj / "node_modules" / "a").mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        for p in (proj / "node_modules", proj / "package.json", proj):
            os.utime(p, (OLD, OLD))
        item = self.find(self.scan(), "artifacts", "old-site")
        self.cli("apply", str(item["id"]))
        self.assertFalse((proj / "node_modules").exists())
        self.assertTrue((proj / "package.json").exists())


if __name__ == "__main__":
    unittest.main()
