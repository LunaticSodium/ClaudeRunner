"""
Integration tests for Docker OAuth mount configuration.

These tests require Docker Desktop to be running and the
``claude-runner-base:latest`` image to be present.  They are skipped
automatically when Docker is unavailable.

Run explicitly with:
    pytest tests/test_docker_oauth_mounts.py -v
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Skip the entire module when Docker is unavailable or the SDK is missing.
# ---------------------------------------------------------------------------

docker = pytest.importorskip("docker", reason="docker SDK not installed")


def _docker_available() -> bool:
    try:
        c = docker.DockerClient(base_url="npipe:////./pipe/docker_engine", timeout=3)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker Desktop not running — skipping Docker integration tests",
)

IMAGE = "claude-runner-base:latest"


@pytest.fixture(scope="module")
def docker_client():
    client = docker.DockerClient(base_url="npipe:////./pipe/docker_engine", timeout=10)
    yield client
    client.close()


def _image_exists(client) -> bool:
    try:
        client.images.get(IMAGE)
        return True
    except docker.errors.ImageNotFound:
        return False


def _run_in_fresh_container(client, cmd: str, mounts: list | None = None) -> tuple[int, str]:
    """
    Run *cmd* (via ``bash -c``) in a temporary container and return
    ``(exit_code, combined_stdout_stderr)``.

    The container is always removed after the command finishes.
    """
    kwargs = dict(
        image=IMAGE,
        command=["bash", "-c", cmd],
        detach=False,
        remove=True,
        stdout=True,
        stderr=True,
        user="1000:1000",
    )
    if mounts:
        kwargs["mounts"] = mounts

    try:
        output = client.containers.run(**kwargs)
        return 0, output.decode(errors="replace") if output else ""
    except docker.errors.ContainerError as exc:
        return exc.exit_status, exc.stderr.decode(errors="replace") if exc.stderr else ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOAuthTmpfsMounts:
    """Verify that tmpfs overlays on ~/.claude subdirs behave correctly."""

    @pytest.fixture(autouse=True)
    def require_image(self, docker_client):
        if not _image_exists(docker_client):
            pytest.skip(f"Docker image {IMAGE!r} not present — skipping mount tests")

    def _make_mounts(self) -> list:
        """Build the same tmpfs mounts that DockerSandbox creates in OAuth mode."""
        _claude_tmpfs = [
            ("/home/claude/.claude/projects",   256 * 1024 * 1024),
            ("/home/claude/.claude/todos",        32 * 1024 * 1024),
            ("/home/claude/.claude/statsig",      32 * 1024 * 1024),
            ("/home/claude/.claude/__pycache__",  16 * 1024 * 1024),
        ]
        return [
            docker.types.Mount(
                target=target,
                source=None,
                type="tmpfs",
                read_only=False,
                tmpfs_size=size,
                tmpfs_mode=0o777,
            )
            for target, size in _claude_tmpfs
        ]

    def test_write_to_projects_subdir_succeeds(self, docker_client):
        """Writing into the tmpfs-overlaid projects/ directory must succeed."""
        mounts = self._make_mounts()
        cmd = (
            "mkdir -p /home/claude/.claude/projects && "
            "echo test > /home/claude/.claude/projects/probe.txt && "
            "cat /home/claude/.claude/projects/probe.txt"
        )
        exit_code, output = _run_in_fresh_container(docker_client, cmd, mounts=mounts)
        assert exit_code == 0, f"Expected exit 0, got {exit_code}. Output: {output}"
        assert "test" in output, f"Expected 'test' in output, got: {output!r}"

    def test_write_to_todos_subdir_succeeds(self, docker_client):
        """Writing into the tmpfs-overlaid todos/ directory must succeed."""
        mounts = self._make_mounts()
        cmd = (
            "mkdir -p /home/claude/.claude/todos && "
            "echo todo-item > /home/claude/.claude/todos/item.txt && "
            "cat /home/claude/.claude/todos/item.txt"
        )
        exit_code, output = _run_in_fresh_container(docker_client, cmd, mounts=mounts)
        assert exit_code == 0, f"Expected exit 0, got {exit_code}. Output: {output}"
        assert "todo-item" in output, f"Expected 'todo-item' in output, got: {output!r}"

    def test_write_to_parent_claude_dir_fails(self, docker_client):
        """
        Writing directly into /home/claude/.claude (the read-only parent) must
        fail while writing into the tmpfs-overlaid projects/ subdirectory must
        succeed.

        We simulate the DockerSandbox OAuth mount strategy: bind the parent
        read-only, pre-create the 'projects' subdirectory so Docker can use it
        as a tmpfs mountpoint, then verify the security boundary holds.
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-create 'projects/' so it exists as a mountpoint inside the
            # read-only bind mount (Docker cannot create it at runtime when the
            # parent is read-only — this mirrors what DockerSandbox.setup() does).
            (Path(tmpdir) / "projects").mkdir()

            bind_mount = docker.types.Mount(
                target="/home/claude/.claude",
                source=tmpdir,
                type="bind",
                read_only=True,
            )
            tmpfs_projects = docker.types.Mount(
                target="/home/claude/.claude/projects",
                source=None,
                type="tmpfs",
                read_only=False,
                tmpfs_size=32 * 1024 * 1024,
                tmpfs_mode=0o777,
            )

            # Writing to projects/ (tmpfs overlay) must succeed.
            cmd_projects = (
                "echo ok > /home/claude/.claude/projects/probe.txt && "
                "cat /home/claude/.claude/projects/probe.txt"
            )
            exit_code, output = _run_in_fresh_container(
                docker_client, cmd_projects, mounts=[bind_mount, tmpfs_projects]
            )
            assert exit_code == 0, f"projects/ write failed (exit {exit_code}): {output}"
            assert "ok" in output, f"Expected 'ok' in output, got: {output!r}"

            # Writing directly into the read-only parent bind must fail.
            cmd_parent = "echo x > /home/claude/.claude/probe.txt"
            exit_code_parent, _ = _run_in_fresh_container(
                docker_client, cmd_parent, mounts=[bind_mount, tmpfs_projects]
            )
            assert exit_code_parent != 0, (
                "Expected non-zero exit when writing to the read-only parent "
                f"/home/claude/.claude, but got exit code {exit_code_parent}"
            )
