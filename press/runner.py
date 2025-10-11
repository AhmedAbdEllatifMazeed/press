import json
from contextlib import suppress
from pathlib import Path

import frappe
import wrapt
from ansible import constants, context
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.executor.task_executor import TaskExecutor
from ansible.inventory.manager import InventoryManager
from ansible.module_utils.common.collections import ImmutableDict
from ansible.parsing.dataloader import DataLoader
from ansible.playbook import Playbook
from ansible.plugins.action.async_status import ActionModule
from ansible.plugins.callback import CallbackBase
from ansible.utils.display import Display
from ansible.vars.manager import VariableManager
from frappe.utils import cstr
from frappe.utils import now_datetime as now

from press.utils import log_error


def reconnect_on_failure():
    @wrapt.decorator
    def wrapper(wrapped, instance, args, kwargs):
        try:
            return wrapped(*args, **kwargs)
        except Exception as e:
            if frappe.db.is_interface_error(e):
                frappe.db.connect()
                return wrapped(*args, **kwargs)
            raise

    return wrapper


class AnsibleCallback(CallbackBase):
        def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

        @reconnect_on_failure()
        def process_task_success(self, result):
                result, action = frappe._dict(result._result), result._task.action
                if action == "user":
                        server_type, server = frappe.db.get_value(
                                "Ansible Play", self.play, ["server_type", "server"]
                        )
                        server = frappe.get_doc(server_type, server)
                        if result.name == "root":
                                server.root_public_key = result.ssh_public_key
                        elif result.name == "frappe":
                                server.frappe_public_key = result.ssh_public_key
                        server.save()

        def v2_runner_on_ok(self, result, *args, **kwargs):
                self.update_task("Success", result)
                self.process_task_success(result)

        def v2_runner_on_failed(self, result, *args, **kwargs):
                self.update_task("Failure", result)

        def v2_runner_on_skipped(self, result):
                self.update_task("Skipped", result)

        def v2_runner_on_unreachable(self, result):
                self.update_task("Unreachable", result)

        def v2_playbook_on_task_start(self, task, is_conditional):
                self.update_task("Running", None, task)

        def v2_playbook_on_start(self, playbook):
                self.update_play("Running")

        def v2_playbook_on_stats(self, stats):
                self.update_play(None, stats)

        @reconnect_on_failure()
        def update_play(self, status=None, stats=None):
                play = frappe.get_doc("Ansible Play", self.play)
                if stats:
                        # Assume we're running on one host
                        host = next(iter(stats.processed.keys()))
                        play.update(stats.summarize(host))
                        if play.failures or play.unreachable:
                                play.status = "Failure"
                        else:
                                play.status = "Success"
                        play.end = now()
                        play.duration = play.end - play.start
                else:
                        play.status = status
                        play.start = now()

                play.save()
                frappe.db.commit()

        @reconnect_on_failure()
        def update_task(self, status, result=None, task=None):
                if result:
                        if not result._task._role:
                                return
                        task_name, result = self.parse_result(result)
                else:
                        if not task._role:
                                return
                        task_name = self.tasks[task._role.get_name()][task.name]
                task = frappe.get_doc("Ansible Task", task_name)
                task.status = status
                if result:
                        task.output = result.stdout
                        task.error = result.stderr
                        task.exception = result.msg
                        # Reduce clutter be removing keys already shown elsewhere
                        for key in ("stdout", "stdout_lines", "stderr", "stderr_lines", "msg"):
                                result.pop(key, None)
                        task.result = json.dumps(result, indent=4)
                        task.end = now()
                        task.duration = task.end - task.start
                else:
                        task.start = now()
                task.save()
                self.publish_play_progress(task.name)
                frappe.db.commit()

        def publish_play_progress(self, task):
                frappe.publish_realtime(
                        "ansible_play_progress",
                        {
                                "progress": self.task_list.index(task),
                                "total": len(self.task_list),
                                "play": self.play,
                        },
                        doctype="Ansible Play",
                        docname=self.play,
                        user=frappe.session.user,
                )

        def parse_result(self, result):
                task = result._task.name
                role = result._task._role.get_name()
                return self.tasks[role][task], frappe._dict(result._result)

        @reconnect_on_failure()
        def on_async_start(self, role, task, job_id):
                task_name = self.tasks[role][task]
                task = frappe.get_doc("Ansible Task", task_name)
                task.job_id = job_id
                task.save()
                frappe.db.commit()

        @reconnect_on_failure()
        def on_async_poll(self, result):
                job_id = result["ansible_job_id"]
                task_name = frappe.get_value(
                        "Ansible Task", {"play": self.play, "job_id": job_id}, "name"
                )
                task = frappe.get_doc("Ansible Task", task_name)
                task.result = json.dumps(result, indent=4)
                task.duration = now() - task.start
                task.save()
                frappe.db.commit()


def _get_runner_log_path(identifier: str) -> Path:
        logs_dir = Path(frappe.get_site_path("logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        scrub = getattr(frappe, "scrub", None)
        if callable(scrub):
                safe_identifier = scrub(identifier)
        else:
                safe_identifier = (
                        identifier.replace(" ", "_")
                        .replace("/", "_")
                        .replace(":", "_")
                        .replace("\\", "_")
                )
        return logs_dir / f"ansible_runner_{safe_identifier}.log"


def _clear_runner_log(identifier: str) -> None:
        try:
                log_path = _get_runner_log_path(identifier)
        except Exception:
                return

        if log_path.exists():
                with suppress(Exception):
                        log_path.unlink()


def _append_runner_log(identifier: str, line: str) -> None:
        with suppress(Exception):
                log_path = _get_runner_log_path(identifier)
                with log_path.open("a", encoding="utf-8") as log_file:
                        log_file.write(line + "\n")


def _log_runner_step(identifier: str, level: str, message: str, *args) -> None:
        try:
                formatted_message = message % args if args else message
        except TypeError:
                formatted_message = message

        timestamp = now().isoformat()
        line = f"{timestamp} [{level.upper()}] {formatted_message}"
        print(line)
        _append_runner_log(identifier, line)


class Ansible:
        def __init__(self, server, playbook, user="root", variables=None, port=22):
                self.server = server
                self.playbook = playbook
                self.playbook_path = frappe.get_app_path("press", "playbooks", self.playbook)
                self.host = f"{server.ip}:{port}"
                self.variables = variables or {}
                server_type = getattr(self.server, "doctype", self.server.__class__.__name__)
                server_name = getattr(self.server, "name", "unknown")
                self._log_identifier = f"{server_type}_{server_name}_{self.playbook}"
                _clear_runner_log(self._log_identifier)
                self._log_step(
                        "step",
                        "Initializing Ansible runner for %s (%s) using playbook %s",
                        server_name,
                        server_type,
                        self.playbook,
                )

                constants.HOST_KEY_CHECKING = False
                context.CLIARGS = ImmutableDict(
                        become_method="sudo",
                        check=False,
                        connection="ssh",
                        # This is the only way to pass variables that preserves newlines
                        extra_vars=[
                                f"{cstr(key)}='{cstr(value)}'" for key, value in self.variables.items()
                        ],
                        remote_user=user,
                        start_at_task=None,
                        syntax=False,
                        verbosity=1,
                )

                self.loader = DataLoader()
                self.passwords = dict({})

                self.sources = f"{self.host},"
                self.inventory = InventoryManager(loader=self.loader, sources=self.sources)
                self.variable_manager = VariableManager(loader=self.loader, inventory=self.inventory)

                self.callback = AnsibleCallback()
                self.display = Display()
                self.display.verbosity = 1
                self.patch()
                self._log_step(
                        "info",
                        "Patched Ansible async handlers for playbook %s on %s",
                        self.playbook,
                        self.host,
                )
                self.create_ansible_play()
                self._log_step(
                        "info",
                        "Prepared Ansible play %s with %s tasks for server %s",
                        self.play,
                        len(self.task_list),
                        server_name,
                )

        def patch(self):
                def modified_action_module_run(*args, **kwargs):
                        result = self.action_module_run(*args, **kwargs)
                        self.callback.on_async_poll(result)
                        return result

                def modified_poll_async_result(executor, result, templar, task_vars=None):
                        job_id = result["ansible_job_id"]
                        task = executor._task
                        self.callback.on_async_start(task._role.get_name(), task.name, job_id)
                        return self._poll_async_result(
                                executor, result, templar, task_vars=task_vars
                        )

                if ActionModule.run.__module__ != "press.runner":
                        self.action_module_run = ActionModule.run
                        ActionModule.run = modified_action_module_run

                if TaskExecutor.run.__module__ != "press.runner":
                        self._poll_async_result = TaskExecutor._poll_async_result
                        TaskExecutor._poll_async_result = modified_poll_async_result

        def unpatch(self):
                if hasattr(self, "_poll_async_result"):
                        TaskExecutor._poll_async_result = self._poll_async_result
                if hasattr(self, "action_module_run"):
                        ActionModule.run = self.action_module_run
                self._log_step("info", "Restored Ansible async handlers for playbook %s", self.playbook)

        def run(self):
                self._log_step(
                        "step",
                        "Configuring Ansible executor for playbook %s targeting %s",
                        self.playbook,
                        self.host,
                )
                try:
                        self.executor = PlaybookExecutor(
                                playbooks=[self.playbook_path],
                                inventory=self.inventory,
                                variable_manager=self.variable_manager,
                                loader=self.loader,
                                passwords=self.passwords,
                        )
                        # Use AnsibleCallback so we can receive updates for tasks execution
                        self.executor._tqm._stdout_callback = self.callback
                        self.callback.play = self.play
                        self.callback.tasks = self.tasks
                        self.callback.task_list = self.task_list
                        self._log_step(
                                "step",
                                "Starting Ansible play %s for server %s",
                                self.play,
                                getattr(self.server, "name", self.host),
                        )
                        return_code = self.executor.run()
                        self._log_step(
                                "info",
                                "Ansible play %s finished with return code %s",
                                self.play,
                                return_code,
                        )
                except Exception as exc:
                        error_message = (
                                frappe.get_traceback(with_context=True)
                                or str(exc)
                                or "Ansible runner execution failed"
                        )
                        self._log_step(
                                "error",
                                "Ansible play %s failed: %s",
                                getattr(self, "play", self.playbook),
                                error_message,
                        )
                        server_data = (
                                self.server.as_dict()
                                if hasattr(self.server, "as_dict")
                                else {"name": getattr(self.server, "name", None)}
                        )
                        log_error(
                                "Ansible Runner Exception",
                                server=server_data,
                                play=getattr(self, "play", None),
                                playbook=self.playbook,
                                error_message=str(exc),
                                traceback=error_message,
                        )
                        raise
                finally:
                        self.unpatch()
                play_doc = frappe.get_doc("Ansible Play", self.play)
                self._log_step(
                        "info",
                        "Ansible play %s completed with status %s",
                        play_doc.name,
                        getattr(play_doc, "status", None),
                )
                return play_doc

        def create_ansible_play(self):
                # Parse the playbook and create Ansible Tasks so we can show how many tasks are pending
                self._log_step(
                        "step",
                        "Loading Ansible playbook %s for server %s",
                        self.playbook_path,
                        getattr(self.server, "name", self.host),
                )
                playbook = Playbook.load(
                        self.playbook_path, variable_manager=self.variable_manager, loader=self.loader
                )
                # Assume we only have one play per playbook
                play = playbook.get_plays()[0]
                play_doc = frappe.get_doc(
                        {
                                "doctype": "Ansible Play",
                                "server_type": self.server.doctype,
                                "server": self.server.name,
                                "variables": json.dumps(self.variables, indent=4),
                                "playbook": self.playbook,
                                "play": play.get_name(),
                        }
                ).insert()
                self.play = play_doc.name
                self.tasks = {}
                self.task_list = []
                self._log_step(
                        "info",
                        "Created Ansible play document %s for playbook %s",
                        play_doc.name,
                        self.playbook,
                )
                for role in play.get_roles():
                        self._log_step("info", "Registering tasks for role %s", role.get_name())
                        for block in role.get_task_blocks():
                                for task in block.block:
                                        task_doc = frappe.get_doc(
                                                {
                                                        "doctype": "Ansible Task",
                                                        "play": self.play,
                                                        "role": role.get_name(),
                                                        "task": task.name,
                                                }
                                        ).insert()
                                        self.tasks.setdefault(role.get_name(), {})[task.name] = task_doc.name
                                        self.task_list.append(task_doc.name)
                self._log_step(
                        "info",
                        "Registered %s Ansible tasks for play %s",
                        len(self.task_list),
                        self.play,
                )

        def _log_step(self, level: str, message: str, *args) -> None:
                identifier = getattr(self, "_log_identifier", None)
                if not identifier:
                        return

                _log_runner_step(identifier, level, message, *args)
