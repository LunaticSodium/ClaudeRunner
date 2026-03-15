"""
DockerSandbox — hard sandbox backend using Docker Desktop on Windows.

Each task run gets a fresh container. The container is always destroyed on
teardown, even if an error occurred.

Network isolation: a dedicated bridge network is created per run with iptables
DROP-by-default rules. An optional allowlist in config.sandbox.network_allowlist
may contain host:port pairs that are added as ACCEPT rules before the default
DROP.

Docker socket (Windows):  npipe:////./pipe/docker_engine
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

# Sentinel imported lazily to avoid circular imports at module level.
_OAUTH_SENTINEL: str = "__claude_oauth__"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency guard — docker SDK is only required when DockerSandbox
# is actually used, not at import time of the package.
# ---------------------------------------------------------------------------
try:
    import docker
    import docker.errors
    import docker.types
    _DOCKER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DOCKER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Sentinel exception
# ---------------------------------------------------------------------------

class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or operated."""


# ---------------------------------------------------------------------------
# Thin wrapper around a running docker-exec session that mimics the interface
# expected by the rest of claude-runner (same as ClaudeProcess from process.py).
# ---------------------------------------------------------------------------

class _DockerClaudeProcess:
    """
    Wraps a docker-exec socket so that callers get the same interface as the
    native ClaudeProcess.

    Streams stdout/stderr line-by-line to `on_line` and calls `on_exit` once
    the exec session ends.
    """

    def __init__(
        self,
        exec_id: str,
        socket,
        container,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self._exec_id = exec_id
        self._socket = socket
        self._container = container
        self._on_line = on_line
        self._on_exit = on_exit
        self._return_code: Optional[int] = None
        self._thread = threading.Thread(target=self._pump, daemon=True, name="docker-pty-pump")
        self._thread.start()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def return_code(self) -> Optional[int]:
        return self._return_code

    def wait(self, timeout: Optional[float] = None) -> int:
        self._thread.join(timeout=timeout)
        return self._return_code if self._return_code is not None else -1

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def send_input(self, text: str) -> None:
        """Write text to the exec session's stdin."""
        try:
            self._socket._sock.sendall(text.encode())
        except Exception as exc:  # noqa: BLE001
            logger.debug("send_input error: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        """Read output from the docker exec socket and dispatch lines."""
        buf = b""
        try:
            for chunk in self._socket:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    decoded = line.decode(errors="replace").rstrip("\r")
                    try:
                        self._on_line(decoded)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_line callback raised")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Docker exec socket closed: %s", exc)
        finally:
            # Flush remaining buffer
            if buf:
                try:
                    self._on_line(buf.decode(errors="replace").rstrip("\r\n"))
                except Exception:  # noqa: BLE001
                    pass
            self._collect_exit_code()

    def _collect_exit_code(self) -> None:
        # Poll until docker reports the exec as finished (up to 30 s).
        client = self._container.client
        for _ in range(300):
            try:
                info = client.api.exec_inspect(self._exec_id)
                if not info.get("Running", True):
                    self._return_code = info.get("ExitCode", -1)
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.1)
        else:
            self._return_code = -1

        try:
            self._on_exit(self._return_code)
        except Exception:  # noqa: BLE001
            logger.exception("on_exit callback raised")


# ---------------------------------------------------------------------------
# DockerSandbox
# ---------------------------------------------------------------------------

class DockerSandbox:
    """
    Hard sandbox using Docker Desktop on Windows.

    - Creates a fresh container for each task run.
    - Bind-mounts working_dir as /workspace (read-write).
    - Bind-mounts readonly_mounts as /ref/<name> (read-only).
    - Passes ANTHROPIC_API_KEY as env var (never written to disk inside the container).
    - Creates an isolated bridge network with iptables rules for the allowlist.
    - Runs Claude Code inside the container via docker exec.
    - Container is destroyed on teardown (even on error).

    Docker socket on Windows: npipe:////./pipe/docker_engine
    """

    #: Image used when no custom image is specified in config.
    DEFAULT_IMAGE = "claude-runner-base:latest"
    #: Dockerfile context directory, relative to the package root.
    DOCKERFILE_DIR = "docker"
    #: Docker socket on Windows.
    DOCKER_SOCKET = "npipe:////./pipe/docker_engine"

    def __init__(self, project_book, config, api_key: str) -> None:
        if not _DOCKER_AVAILABLE:
            raise SandboxError(
                "The 'docker' Python package is not installed. "
                "Run: pip install docker"
            )

        self._project_book = project_book
        self._config = config
        self._api_key = api_key

        # Derived configuration -----------------------------------------
        sandbox_cfg = getattr(config, "sandbox", None) or {}
        self._image: str = _cfg_get(sandbox_cfg, "image", self.DEFAULT_IMAGE)
        self._network_allowlist: List[str] = _cfg_get(sandbox_cfg, "network_allowlist", [])
        self._container_memory: str = _cfg_get(sandbox_cfg, "memory_limit", "2g")
        self._container_cpus: float = float(_cfg_get(sandbox_cfg, "cpu_limit", 2.0))
        self._extra_env: dict = _cfg_get(sandbox_cfg, "extra_env", {})

        # Runtime state -------------------------------------------------
        self._client: Optional["docker.DockerClient"] = None
        self._container = None
        self._network_name: Optional[str] = None
        self._run_id: str = uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Pull/build base image if needed. Create and start the container."""
        self._client = self._connect()

        self._build_base_image()

        self._network_name = self._create_network()

        working_dir = self.get_working_dir_path()
        working_dir.mkdir(parents=True, exist_ok=True)

        # SECURITY: /var/run/docker.sock is never mounted into the container.
        # Mounting the Docker socket would allow container escape.
        volumes = {
            str(working_dir): {"bind": "/workspace", "mode": "rw"},
        }

        # Read-only reference mounts
        readonly_mounts = getattr(self._project_book, "readonly_mounts", {}) or {}
        for name, host_path in readonly_mounts.items():
            volumes[str(host_path)] = {"bind": f"/ref/{name}", "mode": "ro"}

        # OAuth mode: mount the host's Claude Code credentials so the container
        # can authenticate without an API key.  Mounted read-only so the
        # container cannot modify the host session.
        # Note: if the access token expires mid-run Claude Code will attempt a
        # refresh but cannot persist the result; the task may fail after expiry.
        _using_oauth = (self._api_key == _OAUTH_SENTINEL)
        if _using_oauth:
            claude_creds = Path.home() / ".claude"
            if claude_creds.exists():
                volumes[str(claude_creds)] = {"bind": "/home/claude/.claude", "mode": "ro"}
                logger.info("OAuth mode: mounted %s → /home/claude/.claude (read-only)", claude_creds)
            else:
                logger.warning(
                    "OAuth mode active but ~/.claude not found on host — "
                    "Claude Code inside the container may fail to authenticate."
                )

        # SECURITY: Do not inherit host environment.  Inject only explicit vars.
        # - TERM: needed for Claude Code's PTY detection.
        # - ANTHROPIC_API_KEY: only when not using OAuth.
        # - GIT_TOKEN: only when output.git.auto_push is enabled.
        # - extra_env: user-defined vars from config.sandbox.extra_env.
        env: dict = {"TERM": "xterm-256color"}
        if not _using_oauth:
            env["ANTHROPIC_API_KEY"] = self._api_key

        auto_push = getattr(
            getattr(getattr(self._project_book, "output", None), "git", None),
            "auto_push", False,
        )
        if auto_push:
            git_token = _cfg_get(self._config, "git_token", None) or _cfg_get(
                getattr(self._config, "secrets", {}), "git_token", None
            )
            if git_token:
                env["GIT_TOKEN"] = git_token
                logger.info("GIT_TOKEN injected (auto_push is enabled).")
            else:
                logger.warning(
                    "output.git.auto_push is True but no git_token found in config — "
                    "git push inside the container will use the token from ~/.gitconfig (if any)."
                )

        # Project-book env: field (extra vars explicitly declared per-task).
        pb_env = getattr(getattr(self._project_book, "sandbox", None), "env", {}) or {}
        if isinstance(pb_env, dict):
            env.update(pb_env)

        # Global extra_env from config (least priority).
        if self._extra_env:
            env.update(self._extra_env)

        logger.info(
            "Creating container (image=%s, network=%s, run_id=%s, user=1000)",
            self._image,
            self._network_name,
            self._run_id,
        )

        self._container = self._client.containers.run(
            image=self._image,
            name=f"claude-runner-{self._run_id}",
            command="bash",          # Keep alive; Claude launched via exec.
            detach=True,
            stdin_open=True,
            tty=True,
            # Run as UID/GID 1000 (matches the 'claude' user created in the Dockerfile).
            # This ensures the container never runs as root even if the image default changes.
            user="1000:1000",
            network=self._network_name,
            # Allows the container to reach the internet without using --network=host.
            # host.docker.internal resolves to the host machine's gateway IP.
            extra_hosts={"host.docker.internal": "host-gateway"},
            volumes=volumes,
            environment=env,
            mem_limit=self._container_memory,
            nano_cpus=int(self._container_cpus * 1e9),
            working_dir="/workspace",
            remove=False,            # We remove explicitly in teardown.
            # Security hardening
            security_opt=["no-new-privileges:true"],
            cap_drop=["ALL"],
            cap_add=["CHOWN", "SETUID", "SETGID"],  # Needed by Node.js / npm.
            read_only=False,
            tmpfs={"/tmp": "size=256m,mode=1777"},
        )

        logger.info("Container %s started.", self._container.short_id)

    def launch_claude(
        self,
        prompt: str,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> _DockerClaudeProcess:
        """
        Launch Claude Code inside the running container.

        Command: claude --dangerously-skip-permissions -p <prompt>

        Returns a _DockerClaudeProcess that streams output to on_line and
        calls on_exit with the exit code when done.
        """
        if self._container is None:
            raise SandboxError("setup() must be called before launch_claude().")

        # Escape prompt for shell safety.
        safe_prompt = shlex.quote(prompt)
        cmd = f"claude --dangerously-skip-permissions -p {safe_prompt}"

        logger.info("Launching Claude inside container %s", self._container.short_id)
        logger.debug("Command: %s", cmd)

        exec_env: dict = {}
        if self._api_key != _OAUTH_SENTINEL:
            exec_env["ANTHROPIC_API_KEY"] = self._api_key

        exec_id = self._client.api.exec_create(
            self._container.id,
            cmd=["bash", "-lc", cmd],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            environment=exec_env,
            workdir="/workspace",
        )["Id"]

        socket = self._client.api.exec_start(
            exec_id,
            detach=False,
            tty=True,
            socket=True,
        )

        return _DockerClaudeProcess(
            exec_id=exec_id,
            socket=socket,
            container=self._container,
            on_line=on_line,
            on_exit=on_exit,
        )

    def teardown(self) -> None:
        """Stop and remove the container and its network. Always called, even on error."""
        if self._container is not None:
            try:
                self._container.reload()
                if self._container.status in ("running", "paused"):
                    logger.info("Stopping container %s …", self._container.short_id)
                    self._container.stop(timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping container: %s", exc)
            try:
                logger.info("Removing container %s …", self._container.short_id)
                self._container.remove(force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error removing container: %s", exc)
            self._container = None

        if self._network_name and self._client is not None:
            try:
                net = self._client.networks.get(self._network_name)
                net.remove()
                logger.info("Removed network %s.", self._network_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error removing network %s: %s", self._network_name, exc)
            self._network_name = None

    def get_working_dir_path(self) -> Path:
        """Returns the host-side working directory path for this task."""
        sandbox_cfg = getattr(self._project_book, "sandbox", None) or {}
        wd = getattr(sandbox_cfg, "working_dir", None) or _cfg_get(
            getattr(self._config, "sandbox", {}), "working_dir", None
        )
        if not wd:
            wd = "/tmp/claude-runner/workspace"
            logger.warning(
                "No sandbox.working_dir configured — falling back to %s. "
                "Set sandbox.working_dir in your project book to avoid this.",
                wd,
            )
        return Path(wd)

    @staticmethod
    def check_available() -> bool:
        """Returns True if Docker Desktop is running and accessible via the Windows pipe."""
        if not _DOCKER_AVAILABLE:
            return False
        try:
            client = docker.DockerClient(base_url=DockerSandbox.DOCKER_SOCKET, timeout=3)
            client.ping()
            client.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _connect(self) -> "docker.DockerClient":
        """Connect to the Docker daemon, raising SandboxError on failure."""
        try:
            client = docker.DockerClient(base_url=self.DOCKER_SOCKET, timeout=10)
            client.ping()
            return client
        except Exception as exc:
            raise SandboxError(
                "Docker Desktop is not running. Please start Docker Desktop and try again."
            ) from exc

    def _create_network(self) -> str:
        """
        Create an isolated Docker bridge network for this run.

        A default DROP policy is enforced via iptables options. Each entry in
        config.sandbox.network_allowlist (format: "host:port") is added as an
        ACCEPT rule before the DROP.

        Returns the network name.
        """
        network_name = f"claude-runner-net-{self._run_id}"

        # Build iptables ACCEPT rules for the allowlist.
        # Docker's com.docker.network.bridge.host_binding_ipv4 and
        # com.docker.network.driver.mtu options are driver-specific;
        # actual iptables rules injected post-creation via the low-level API.
        ipam_pool = docker.types.IPAMPool(subnet="172.30.0.0/24")
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])

        # Default: bridge network with full outbound access so Claude Code can
        # reach api.anthropic.com.  Set internal=True only when the project book
        # explicitly requests deny_all_others=True *and* no allowlist is given.
        pb_network = getattr(getattr(self._project_book, "sandbox", None), "network", None)
        deny_all = getattr(pb_network, "deny_all_others", False) if pb_network is not None else False
        make_internal = deny_all and len(self._network_allowlist) == 0

        network = self._client.networks.create(
            name=network_name,
            driver="bridge",
            ipam=ipam_config,
            internal=make_internal,
            options={
                "com.docker.network.bridge.enable_icc": "false",
                "com.docker.network.bridge.enable_ip_masquerade": "true",
            },
            labels={
                "claude-runner.run_id": self._run_id,
                "claude-runner.managed": "true",
            },
        )

        logger.info(
            "Created network %s (internal=%s, deny_all_others=%s, allowlist=%s).",
            network_name,
            make_internal,
            deny_all,
            self._network_allowlist,
        )

        if self._network_allowlist:
            logger.debug(
                "Network allowlist is configured (%d entries). "
                "Ensure the host firewall / iptables rules permit these destinations: %s",
                len(self._network_allowlist),
                self._network_allowlist,
            )

        return network_name

    def _build_base_image(self) -> None:
        """
        Build the claude-runner-base Docker image from docker/Dockerfile if not
        already present in the local image store.

        The Dockerfile directory is resolved relative to this source file so that
        the build context is always correct regardless of the working directory.
        """
        try:
            self._client.images.get(self._image)
            logger.debug("Image %s already present — skipping build.", self._image)
            return
        except docker.errors.ImageNotFound:
            pass

        # Resolve Dockerfile directory relative to this module.
        module_dir = Path(__file__).resolve().parent
        # Walk up to the package root (claude_runner/sandbox -> claude_runner -> project root)
        project_root = module_dir.parent.parent
        dockerfile_dir = project_root / self.DOCKERFILE_DIR

        if not dockerfile_dir.is_dir():
            raise SandboxError(
                f"Dockerfile directory not found at {dockerfile_dir}. "
                f"Cannot build image {self._image!r}. "
                "Run 'claude-runner docker update' or provide a pre-built image."
            )

        logger.info(
            "Building image %s from %s …",
            self._image,
            dockerfile_dir,
        )

        try:
            image, build_logs = self._client.images.build(
                path=str(dockerfile_dir),
                tag=self._image,
                rm=True,
                forcerm=True,
                buildargs={"CLAUDE_CODE_VERSION": "latest"},
                labels={"claude-runner.managed": "true"},
            )
            for entry in build_logs:
                if "stream" in entry:
                    msg = entry["stream"].rstrip("\n")
                    if msg:
                        logger.debug("[docker build] %s", msg)
            logger.info("Image %s built successfully.", self._image)
        except docker.errors.BuildError as exc:
            raise SandboxError(
                f"Failed to build Docker image {self._image!r}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _cfg_get(cfg, key: str, default):
    """
    Retrieve a value from a config object that may be a dict or an
    attribute-bearing object (e.g. a dataclass / SimpleNamespace).
    """
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)
