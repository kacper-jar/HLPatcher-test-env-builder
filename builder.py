import os
import json
import stat
import zipfile
import subprocess
import shutil
from pathlib import Path
import curses
import threading
import queue
import time
import requests
import pexpect

state = {
    "target_dir": "",
    "preset": None,
    "components": [],
    "presets": [],
    "output_dirs": {},
    "selected_components": []
}

ui_queue = queue.Queue()
auth_code_event = threading.Event()
auth_code_result = [""]
install_done = threading.Event()


def draw_header(stdscr, title=None):
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    title_str = " HLPatcher Test Environment Builder "
    stdscr.attron(curses.A_REVERSE)
    stdscr.addstr(0, 0, title_str + " " * (w - len(title_str) - 1))
    stdscr.attroff(curses.A_REVERSE)


def get_input(stdscr, y, x, prompt, hidden=False):
    stdscr.addstr(y, x, prompt)
    stdscr.refresh()
    curses.echo()
    if hidden:
        curses.noecho()
    else:
        curses.echo()

    val = stdscr.getstr(y, x + len(prompt)).decode('utf-8')
    curses.noecho()
    return val


def screen_preset(stdscr):
    presets = state["presets"]
    current_idx = 0
    if state["preset"] in presets:
        current_idx = presets.index(state["preset"])

    while True:
        stdscr.clear()
        draw_header(stdscr, "Preset Selection")
        stdscr.addstr(2, 2, "Select a preset using UP/DOWN keys, press ENTER to confirm:")

        for i, preset in enumerate(presets):
            prefix = "[*]" if i == current_idx else "[ ]"
            if i == current_idx:
                stdscr.attron(curses.A_REVERSE)
            stdscr.addstr(4 + i, 4, f"{prefix} {preset['name']}")
            if i == current_idx:
                stdscr.attroff(curses.A_REVERSE)

        stdscr.refresh()
        key = stdscr.getch()

        if key == curses.KEY_UP and current_idx > 0:
            current_idx -= 1
        elif key == curses.KEY_DOWN and current_idx < len(presets) - 1:
            current_idx += 1
        elif key == 10 or key == 13:
            state["preset"] = presets[current_idx]
            break


def screen_setup(stdscr):
    current_item = 0
    items = ["Path", "Preset"]

    if not state["preset"] and state["presets"]:
        state["preset"] = state["presets"][0]

    while True:
        stdscr.clear()
        draw_header(stdscr, "Configuration")

        preset = state["preset"]
        rule = preset["rule"] if preset else None
        selected_components = []
        if rule:
            for comp in state["components"]:
                tags = set(comp.get("tags", []))
                if rule == "all":
                    selected_components.append(comp)
                else:
                    req = set(rule.get("require", []))
                    any_of = set(rule.get("any_of", []))
                    if req and not req.issubset(tags):
                        continue
                    if any_of and not any_of.intersection(tags):
                        continue
                    selected_components.append(comp)

        state["selected_components"] = selected_components

        stdscr.addstr(2, 2, "Configuration", curses.A_BOLD)

        for i, item in enumerate(items):
            if i == current_item:
                stdscr.attron(curses.A_REVERSE)

            if item == "Path":
                text = f"Target Path: {state['target_dir'] if state['target_dir'] else '<Not Set, Press ENTER to edit>'}"
            elif item == "Preset":
                preset_name = preset["name"] if preset else "None"
                text = f"Preset: {preset_name}"

            stdscr.addstr(4 + i, 4, text)

            if i == current_item:
                stdscr.attroff(curses.A_REVERSE)

        h, w = stdscr.getmaxyx()

        stdscr.addstr(h - 2, 2, "UP/DOWN: Navigate | ENTER: Select/Edit | F1: Start | F4: Quit")

        right_col = w // 2
        if right_col > 25:
            stdscr.addstr(2, right_col, "Summary", curses.A_BOLD)
            stdscr.addstr(4, right_col, "Components to install:")
            max_summary_lines = h - 3 - 5

            if max_summary_lines > 0:
                disp_comps = selected_components[:max_summary_lines]
                if len(selected_components) > max_summary_lines:
                    disp_comps = selected_components[:max_summary_lines - 1]

                for i, c in enumerate(disp_comps):
                    text = f"- {c['title']} ({len(c['steps'])} steps)"
                    max_len = w - right_col - 2
                    if len(text) > max_len and max_len > 0:
                        text = text[:max_len - 3] + "..."
                    stdscr.addstr(5 + i, right_col, text)

                if len(selected_components) > len(disp_comps):
                    stdscr.addstr(5 + len(disp_comps), right_col,
                                  f"... and {len(selected_components) - len(disp_comps)} more")
        else:
            stdscr.addstr(7, 2, "Summary", curses.A_BOLD)
            stdscr.addstr(8, 2, "Components to install:")
            max_summary_lines = h - 3 - 9
            if max_summary_lines > 0:
                disp_comps = selected_components[:max_summary_lines - 1]
                for i, c in enumerate(disp_comps):
                    stdscr.addstr(9 + i, 4, f"- {c['title']} ({len(c['steps'])} steps)")
                if len(selected_components) > len(disp_comps):
                    stdscr.addstr(9 + len(disp_comps), 4, f"... and {len(selected_components) - len(disp_comps)} more")

        stdscr.refresh()
        key = stdscr.getch()

        if key == curses.KEY_UP and current_item > 0:
            current_item -= 1
        elif key == curses.KEY_DOWN and current_item < len(items) - 1:
            current_item += 1
        elif key == curses.KEY_F1:
            if not state["target_dir"]:
                stdscr.addstr(h - 3, 2, "Please set a Target Path first!", curses.A_BOLD)
                stdscr.refresh()
                curses.napms(1000)
            elif state["target_dir"] and state["preset"]:
                break
        elif key == curses.KEY_F4:
            state["target_dir"] = ""
            break
        elif key == 10 or key == 13:
            if current_item == 0:
                stdscr.move(4 + 0, 4)
                stdscr.clrtoeol()
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
                val = get_input(stdscr, 4 + 0, 4, "Enter new path: ")
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                if val.strip():
                    state["target_dir"] = os.path.expanduser(val.strip())
            elif current_item == 1:
                screen_preset(stdscr)


def run_installation_thread():
    components = state["selected_components"]
    target_dir = Path(state["target_dir"])
    temp_dir = target_dir / "current-download"

    target_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    depot_cmd = Path("./tools/depotdownloader/DepotDownloader").absolute()

    ui_queue.put(("overall_total", len(components)))
    ui_queue.put(("overall_progress", 0))

    for comp_idx, comp in enumerate(components):
        comp_dir = temp_dir / comp["foldername"]
        comp_dir.mkdir(parents=True, exist_ok=True)
        ui_queue.put(("current_game", comp['title']))
        ui_queue.put(("log", f"Installing {comp['title']}..."))

        steps = comp.get("steps", [])
        ui_queue.put(("task_total", len(steps)))
        ui_queue.put(("task_progress", 0))

        for step_idx, step in enumerate(steps):
            stype = step.get("type")
            ui_queue.put(("log", f"  Step {step_idx + 1}/{len(steps)}: {stype}"))

            if stype == "download":
                url = step.get("url")
                ui_queue.put(("log", f"  Downloading ZIP from {url}..."))
                r = requests.get(url, stream=True)
                zip_path = comp_dir / "temp.zip"
                with open(zip_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                ui_queue.put(("log", "  Extracting ZIP..."))
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(comp_dir)
                zip_path.unlink()

            elif stype == "steam":
                app_id = step.get("app")
                depots = step.get("depots", [])
                branch = step.get("branch")

                download_tasks = []
                if depots:
                    for depot in depots:
                        args = ["-app", str(app_id), "-depot", str(depot['id']), "-manifest", str(depot['manifest'])]
                        if branch:
                            args.extend(["-beta", branch])
                        download_tasks.append({
                            "msg": f"  Steam Download: App {app_id}, Depot {depot['id']}" + (
                                f", Branch {branch}" if branch else ""),
                            "args": args
                        })
                else:
                    args = ["-app", str(app_id)]
                    if branch:
                        args.extend(["-beta", branch])
                    download_tasks.append({
                        "msg": f"  Steam Download: App {app_id} (All Depots)" + (
                            f", Branch {branch}" if branch else ""),
                        "args": args
                    })

                for task in download_tasks:
                    ui_queue.put(("log", task["msg"]))

                    cmd = [
                              str(depot_cmd),
                              "-dir", str(comp_dir),
                              "-qr"
                          ] + task["args"]

                    ui_queue.put(("log", f"  Running DepotDownloader..."))
                    try:
                        child = pexpect.spawn(" ".join(cmd), encoding='utf-8', timeout=None)
                        while True:
                            index = child.expect([
                                "Please enter your 2-factor auth code",
                                "Please enter the authentication code sent to your email address",
                                pexpect.EOF,
                                pexpect.TIMEOUT,
                                "\r\n"
                            ], timeout=0.1)

                            if index == 0 or index == 1:
                                ui_queue.put(("log", "  Steam Guard Code requested!"))
                                ui_queue.put(("request_2fa", ""))

                                auth_code_event.wait()
                                child.sendline(auth_code_result[0])
                                auth_code_event.clear()

                                ui_queue.put(("log", "  Sent Steam Guard Code."))
                            elif index == 2:
                                break
                            elif index == 4:
                                line = child.before
                                if line:
                                    ui_queue.put(("log", line))
                    except Exception as e:
                        ui_queue.put(("log", f"  Error running DepotDownloader: {e}"))

            ui_queue.put(("task_advance", 1))
        ui_queue.put(("overall_advance", 1))

    ui_queue.put(("log", "\nMerging files..."))
    ui_queue.put(("current_game", "Merging Files"))
    output_dirs = state.get("output_dirs", {})

    ui_queue.put(("task_total", max(1, len(output_dirs))))
    ui_queue.put(("task_progress", 0))

    for out_name, src_folders in output_dirs.items():
        out_path = target_dir / out_name
        ui_queue.put(("log", f"Creating {out_name}..."))
        for src in src_folders:
            src_path = temp_dir / src
            if src_path.exists():
                ui_queue.put(("log", f"  Merging {src}..."))
                shutil.copytree(src_path, out_path, dirs_exist_ok=True)
                shutil.rmtree(src_path)
        ui_queue.put(("task_advance", 1))

    ui_queue.put(("log", "Cleaning up current-download..."))
    shutil.rmtree(temp_dir, ignore_errors=True)

    ui_queue.put(("log", "\nInstallation Complete!"))
    install_done.set()


def screen_execution(stdscr):
    stdscr.nodelay(True)

    overall_p = 0
    overall_t = 1
    task_p = 0
    task_t = 1
    current_game = ""
    logs = []

    start_time = time.time()

    t = threading.Thread(target=run_installation_thread, daemon=True)
    t.start()

    while True:
        while True:
            try:
                msg_type, msg_val = ui_queue.get_nowait()
                if msg_type == "overall_total":
                    overall_t = msg_val
                elif msg_type == "overall_progress":
                    overall_p = msg_val
                elif msg_type == "overall_advance":
                    overall_p += msg_val
                elif msg_type == "task_total":
                    task_t = msg_val
                elif msg_type == "task_progress":
                    task_p = msg_val
                elif msg_type == "task_advance":
                    task_p += msg_val
                elif msg_type == "current_game":
                    current_game = msg_val
                elif msg_type == "log":
                    logs.extend(msg_val.split("\n"))
                elif msg_type == "request_2fa":
                    stdscr.nodelay(False)
                    code = get_input(stdscr, 10, 2, "STEAM GUARD CODE REQUIRED: ")
                    auth_code_result[0] = code.strip()
                    auth_code_event.set()
                    stdscr.nodelay(True)
            except queue.Empty:
                break

        draw_header(stdscr, "Execution")

        overall_pct = int((overall_p / overall_t) * 100) if overall_t > 0 else 0
        task_pct = int((task_p / task_t) * 100) if task_t > 0 else 0

        elapsed_s = int(time.time() - start_time)
        mins, secs = divmod(elapsed_s, 60)

        stdscr.addstr(2, 2, f"Overall {overall_pct}%")
        stdscr.addstr(3, 2, f"{current_game} {task_pct}%")
        stdscr.addstr(5, 2, f"Elapsed time: {mins:02d}:{secs:02d}")

        h, w = stdscr.getmaxyx()
        max_log_lines = h - 8

        disp_logs = logs[-max_log_lines:] if len(logs) > max_log_lines else logs
        for i, l in enumerate(disp_logs):
            stdscr.addstr(7 + i, 2, l[:w - 3])

        if install_done.is_set():
            stdscr.addstr(h - 1, 2, "[ Press ENTER to exit ]", curses.A_REVERSE)

        stdscr.refresh()

        key = stdscr.getch()
        if install_done.is_set() and (key == 10 or key == 13):
            break

        time.sleep(0.05)


def setup_tools():
    tools_dir = Path("./tools/depotdownloader")
    dd_path = tools_dir / "DepotDownloader"

    if not dd_path.exists():
        tools_dir.mkdir(parents=True, exist_ok=True)
        url = "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_3.4.0/DepotDownloader-macos-arm64.zip"
        zip_path = tools_dir / "dd.zip"

        r = requests.get(url)
        with open(zip_path, "wb") as f:
            f.write(r.content)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(tools_dir)

        zip_path.unlink()

        if dd_path.exists():
            subprocess.run(["xattr", "-c", str(dd_path)], capture_output=True)
            st = os.stat(dd_path)
            os.chmod(dd_path, st.st_mode | stat.S_IEXEC)


def main(stdscr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    with open("presets.json", "r") as f:
        state["presets"] = json.load(f)
    with open("components.json", "r") as f:
        state["components"] = json.load(f)
    with open("output_dirs.json", "r") as f:
        state["output_dirs"] = json.load(f)

    setup_tools()

    screen_setup(stdscr)
    if not state["target_dir"]:
        return

    screen_execution(stdscr)


if __name__ == "__main__":
    curses.wrapper(main)
