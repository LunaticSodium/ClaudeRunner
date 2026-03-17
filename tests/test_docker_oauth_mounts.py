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


def _run_in_fresh_container(client, cmd: str, mounts: list | None = None, volumes: dict | None = None) -> tuple[int, str]:
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
    if volumes:
        kwargs["volumes"] = volumes

    try:
        output = client.containers.run(**kwargs)
        return 0, output.decode(errors="replace") if output else ""
    except docker.errors.ContainerError as exc:
        return exc.exit_status, exc.stderr.decode(errors="replace") if exc.stderr else ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOAuthWritableCopyMounts:
    """
    Verify that the writable-copy OAuth mount strategy works correctly.

    DockerSandbox copies auth-critical files from ~/.claude into a per-run
    writable directory and bind-mounts that as /home/claude/.claude (rw).
    This avoids the read-only 9p filesystem issue that prevents Claude Code
    from writing runtime state.
    """

    @pytest.fixture(autouse=True)
    def require_image(self, docker_client):
        if not _image_exists(docker_client):
            pytest.skip(f"Docker image {IMAGE!r} not present — skipping mount tests")

    def test_writable_claude_dir_allows_arbitrary_writes(self, docker_client, tmp_path):
        """
        A writable bind-mounted .claude dir must accept writes to any subdir.
        """
        claude_rw = tmp_path / "claude"
        claude_rw.mkdir()

        volumes = {str(claude_rw): {"bind": "/home/claude/.claude", "mode": "rw"}}

        # Write to several subdirs that Claude Code uses at runtime.
        cmd = (
            "mkdir -p /home/claude/.claude/projects /home/claude/.claude/tasks "
            "/home/claude/.claude/cache /home/claude/.claude/telemetry && "
            "echo proj > /home/claude/.claude/projects/probe.txt && "
            "echo task > /home/claude/.claude/tasks/probe.txt && "
            "echo cache > /home/claude/.claude/cache/probe.txt && "
            "echo tele > /home/claude/.claude/telemetry/probe.txt && "
            "cat /home/claude/.claude/projects/probe.txt "
            "/home/claude/.claude/tasks/probe.txt "
            "/home/claude/.claude/cache/probe.txt "
            "/home/claude/.claude/telemetry/probe.txt"
        )
        exit_code, output = _run_in_fresh_container(docker_client, cmd, volumes=volumes)
        assert exit_code == 0, f"Expected exit 0, got {exit_code}. Output: {output}"
        assert "proj" in output
        assert "task" in output
        assert "cache" in output
        assert "tele" in output

    def test_sessions_copied_in_are_readable(self, docker_client, tmp_path):
        """
        Session tokens copied into the writable dir must be readable by the container.
        """
        claude_rw = tmp_path / "claude"
        sessions_dir = claude_rw / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "12345.json").write_text('{"token": "test-token"}', encoding="utf-8")

        volumes = {str(claude_rw): {"bind": "/home/claude/.claude", "mode": "rw"}}

        cmd = "cat /home/claude/.claude/sessions/12345.json"
        exit_code, output = _run_in_fresh_container(docker_client, cmd, volumes=volumes)
        assert exit_code == 0, f"Expected exit 0, got {exit_code}. Output: {output}"
        assert "test-token" in output

    def test_container_writes_do_not_escape_to_host_original(self, docker_client, tmp_path):
        """
        Writes inside the container go to the copy, not back to the host source.
        This is inherent to the copy strategy but worth asserting explicitly.
        """
        # Simulate host source dir (read-only reference)
        host_source = tmp_path / "host_claude"
        host_source.mkdir()
        (host_source / "original.txt").write_text("original", encoding="utf-8")

        # The per-run copy — this is what gets mounted
        claude_rw = tmp_path / "claude"
        claude_rw.mkdir()
        import shutil
        shutil.copy2(host_source / "original.txt", claude_rw / "original.txt")

        volumes = {str(claude_rw): {"bind": "/home/claude/.claude", "mode": "rw"}}

        cmd = "echo modified > /home/claude/.claude/original.txt"
        exit_code, _ = _run_in_fresh_container(docker_client, cmd, volumes=volumes)
        assert exit_code == 0

        # The container modified the copy, not the host source.
        assert (host_source / "original.txt").read_text(encoding="utf-8").strip() == "original"
        # The copy was modified.
        assert "modified" in (claude_rw / "original.txt").read_text(encoding="utf-8")
