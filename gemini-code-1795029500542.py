import os
import csv
import pickle
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import mysql.connector

_CONNECTOR_WORDS_TEXT = """
what which who whom whose when where why how
is are am was were be been being do does did done have has had
can could will would shall should may might must
a an the and or but if then than so as of in on at to from by for with without
about into onto over under between not no
i me my mine we us our you your he him his she her it its they them their
this that these those there here
many much more most some any all every each few lot lots
find search show open get give list look looking want need please tell
file files folder folders named called containing contains
"""


def _load_connector_words_from_text(text):
    connector_words = []
    for line in text.splitlines():
        line = line.strip().lower()
        if line == "":
            continue
        for word in line.split():
            connector_words.append(word)
    return connector_words


def _split_words(query):
    clean = ""
    for ch in query.lower():
        if ch.isalnum():
            clean = clean + ch
        else:
            clean = clean + " "
    return clean.split()


def _main_words(query, connector_words, use_splitting=True):
    if not use_splitting:
        phrase = query.strip().lower()[:50]
        if phrase == "":
            return [], []
        return [phrase], []
    main = []
    ignored = []
    for word in _split_words(query):
        if word in connector_words or len(word) < 2 or len(word) > 50:
            if word not in ignored:
                ignored.append(word)
        elif word not in main:
            main.append(word)
    return main, ignored


MAX_RESULTS = 2000
PROGRESS_EVERY = 100

SYSTEM_FOLDERS = [
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "system volume information", "$recycle.bin", "node_modules", "__pycache__",
]

FILE_TYPES = {
    "Document": ["pdf", "doc", "docx", "txt", "rtf", "odt", "ppt", "pptx", "xls", "xlsx", "csv", "md"],
    "Picture":  ["jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "heic", "tif", "tiff"],
    "Audio":    ["mp3", "wav", "aac", "flac", "m4a", "ogg", "wma"],
    "Video":    ["mp4", "mkv", "avi", "mov", "wmv", "webm", "flv", "3gp"],
    "Archive":  ["zip", "rar", "7z", "tar", "gz", "iso"],
    "Program":  ["exe", "msi", "bat", "cmd", "apk"],
    "Code":     ["py", "java", "c", "cpp", "h", "js", "html", "css", "sql", "json", "xml"],
}


def _list_drives():
    drives = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.path.exists(letter + ":\\"):
            drives.append(letter + ":\\")
    return drives


def _file_type(name, is_folder):
    if is_folder:
        return "Folder"
    dot = name.rfind(".")
    if dot == -1:
        return "Other"
    extension = name[dot + 1:].lower()
    for kind in FILE_TYPES:
        if extension in FILE_TYPES[kind]:
            return kind
    return "Other"


def _count_matches(name, words):
    name = name.lower()
    count = 0
    for word in words:
        if word in name:
            count = count + 1
    return count


def _is_system_folder(name):
    name = name.lower()
    return name in SYSTEM_FOLDERS or name[0] == "$" or name[0] == "."


def _size_in_kb(entry, is_folder):
    if is_folder:
        return None
    try:
        return round(entry.stat().st_size / 1024, 1)
    except OSError:
        return None


def _search(paths, words, skip_system=True, progress=None):
    buckets = []
    for n in range(len(words) + 1):
        buckets.append([])
    checked = 0
    total = 0
    stopped = False

    stack = []
    for path in paths[::-1]:
        stack.append(path)

    while len(stack) > 0 and not stopped:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    checked = checked + 1
                    is_folder = entry.is_dir() and not entry.is_symlink()
                    if is_folder and not (skip_system and _is_system_folder(entry.name)):
                        stack.append(entry.path)
                    matched = _count_matches(entry.name, words)
                    if matched > 0:
                        total = total + 1
                        buckets[matched].append([matched, entry.name, folder,
                                                 _file_type(entry.name, is_folder),
                                                 _size_in_kb(entry, is_folder)])
                    if progress is not None and checked % PROGRESS_EVERY == 0:
                        if not progress(checked, total, folder):
                            stopped = True
                            break
        except OSError:
            pass

    matches = []
    n = len(words)
    while n > 0:
        for item in buckets[n]:
            if len(matches) < MAX_RESULTS:
                matches.append(item)
        n = n - 1
    return matches, checked, total, stopped


HOST = "localhost"
USER = "root"
PASSWORD = ""
DATABASE = "file_finder"

_TABLES = [
    """CREATE TABLE IF NOT EXISTS searches (
        search_id    INT PRIMARY KEY,
        query_text   VARCHAR(255) NOT NULL,
        drives       VARCHAR(100) NOT NULL,
        total_found  INT,
        search_date  DATE
    )""",
    """CREATE TABLE IF NOT EXISTS keywords (
        search_id  INT NOT NULL,
        word_no    INT NOT NULL,
        word       VARCHAR(50) NOT NULL,
        PRIMARY KEY (search_id, word_no)
    )""",
    """CREATE TABLE IF NOT EXISTS results (
        search_id  INT NOT NULL,
        item_no    INT NOT NULL,
        item_name  VARCHAR(255) NOT NULL,
        folder     VARCHAR(1000) NOT NULL,
        item_type  VARCHAR(10),
        size_kb    FLOAT,
        matched    INT,
        PRIMARY KEY (search_id, item_no)
    )""",
]


def _db_connect(database=DATABASE):
    try:
        con = mysql.connector.connect(host=HOST, user=USER, password=PASSWORD)
    except mysql.connector.Error as error:
        raise RuntimeError(
            "Cannot connect to MySQL:\n" + str(error) +
            "\n\nIs the MySQL server running? Are HOST, USER and PASSWORD correct?"
        )
    cur = con.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS " + database + " CHARACTER SET utf8mb4")
    cur.execute("USE " + database)
    for statement in _TABLES:
        cur.execute(statement)
    con.commit()
    return con


def _new_search_id(con):
    cur = con.cursor()
    cur.execute("SELECT MAX(search_id) FROM searches")
    biggest = cur.fetchone()[0]
    if biggest is None:
        return 1
    return biggest + 1


def _db_save_search(con, query, paths, words):
    search_id = _new_search_id(con)
    parts = []
    for p in paths:
        if len(p) <= 3 and p.endswith(":\\"):
            parts.append(p[:2])
        else:
            parts.append(p)
    drive_text = " ".join(parts)[:100]
    cur = con.cursor()
    cur.execute("INSERT INTO searches VALUES (%s, %s, %s, %s, CURDATE())",
                (search_id, query[:255], drive_text, 0))
    word_no = 0
    for word in words:
        word_no = word_no + 1
        cur.execute("INSERT INTO keywords VALUES (%s, %s, %s)", (search_id, word_no, word))
    con.commit()
    return search_id


def _db_save_results(con, search_id, matches, total_found):
    cur = con.cursor()
    item_no = 0
    for matched, name, folder, item_type, size_kb in matches:
        if len(folder) > 1000:
            continue
        item_no = item_no + 1
        cur.execute("INSERT INTO results VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (search_id, item_no, name, folder, item_type, size_kb, matched))
    cur.execute("UPDATE searches SET total_found = %s WHERE search_id = %s", (total_found, search_id))
    con.commit()


def _db_get_results(con, search_id, item_type="All"):
    cur = con.cursor()
    if item_type == "All":
        cur.execute("SELECT item_name, folder, item_type, size_kb, matched FROM results "
                    "WHERE search_id = %s ORDER BY matched DESC, item_name", (search_id,))
    else:
        cur.execute("SELECT item_name, folder, item_type, size_kb, matched FROM results "
                    "WHERE search_id = %s AND item_type = %s ORDER BY matched DESC, item_name",
                    (search_id, item_type))
    return cur.fetchall()


def _db_count_by_type(con, search_id):
    cur = con.cursor()
    cur.execute("SELECT item_type, COUNT(*) FROM results WHERE search_id = %s "
                "GROUP BY item_type ORDER BY COUNT(*) DESC, item_type", (search_id,))
    return cur.fetchall()


def _db_get_search(con, search_id):
    cur = con.cursor()
    cur.execute("SELECT query_text, drives, total_found, search_date FROM searches "
                "WHERE search_id = %s", (search_id,))
    return cur.fetchone()


def _db_get_keywords(con, search_id):
    cur = con.cursor()
    cur.execute("SELECT word FROM keywords WHERE search_id = %s ORDER BY word_no", (search_id,))
    result = []
    for row in cur.fetchall():
        result.append(row[0])
    return result


def _db_get_history(con):
    cur = con.cursor()
    cur.execute("SELECT s.search_id, s.query_text, s.drives, s.total_found, s.search_date, k.word "
                "FROM searches s, keywords k "
                "WHERE s.search_id = k.search_id "
                "ORDER BY s.search_id DESC, k.word_no")
    history = []
    for search_id, query, drives, found, date, word in cur.fetchall():
        if len(history) > 0 and history[-1][0] == search_id:
            history[-1][2].append(word)
        else:
            history.append([search_id, query, [word], drives, found, date])
    return history


def _db_top_words(con):
    cur = con.cursor()
    cur.execute("SELECT word, COUNT(*) FROM keywords GROUP BY word HAVING COUNT(*) > 1 "
                "ORDER BY COUNT(*) DESC, word")
    return cur.fetchall()[:5]


def _db_delete_search(con, search_id):
    cur = con.cursor()
    cur.execute("DELETE FROM results WHERE search_id = %s", (search_id,))
    deleted = cur.rowcount
    cur.execute("DELETE FROM keywords WHERE search_id = %s", (search_id,))
    cur.execute("DELETE FROM searches WHERE search_id = %s", (search_id,))
    con.commit()
    return deleted


def _db_clear_history(con):
    cur = con.cursor()
    cur.execute("DELETE FROM results")
    cur.execute("DELETE FROM keywords")
    cur.execute("DELETE FROM searches")
    con.commit()


_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.dat")


def _export_csv(file_name, rows):
    with open(file_name, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Folder", "Full path", "Type", "Size (KB)", "Words matched"])
        for name, folder, item_type, size_kb, matched in rows:
            writer.writerow([name, folder, os.path.join(folder, name), item_type, size_kb, matched])
    return len(rows)


def _save_settings(settings):
    with open(_SETTINGS_FILE, "wb") as f:
        pickle.dump(settings, f)


def _load_settings():
    try:
        with open(_SETTINGS_FILE, "rb") as f:
            data = pickle.load(f)
            return {
                "drives": data.get("drives", []),
                "folders": data.get("folders", []),
                "skip_system": data.get("skip_system", True),
                "split_words": data.get("split_words", True),
            }
    except (FileNotFoundError, EOFError, pickle.UnpicklingError, AttributeError):
        return {"drives": [], "folders": [], "skip_system": True, "split_words": True}


BG     = "#f5f6f8"
WHITE  = "#ffffff"
ACCENT = "#2563eb"
DARK   = "#1f2937"
GREY   = "#6b7280"
FONT   = "Segoe UI"

con              = None
connector_words  = []
current_search   = 0
current_words    = []
custom_folders   = []
scanning         = False
stop_requested   = False
force_stopped    = False
close_requested  = False
history_window   = None
history_table    = None


def chosen_drives():
    drives = []
    for drive in drive_vars:
        if drive_vars[drive].get():
            drives.append(drive)
    return drives


def chosen_paths():
    paths = []
    for d in chosen_drives():
        paths.append(d)
    for folder in custom_folders:
        if os.path.exists(folder) and folder not in paths:
            paths.append(folder)
    return paths


def update_folders_label():
    if len(custom_folders) == 0:
        folders_text.set("No custom folders selected")
    else:
        folders_text.set("Custom folders:  " + "  |  ".join(custom_folders))


def add_custom_folder():
    folder = filedialog.askdirectory(title="Select folder from any drive")
    if folder != "":
        folder = os.path.normpath(folder)
        if folder not in custom_folders:
            custom_folders.append(folder)
            update_folders_label()


def clear_custom_folders():
    custom_folders.clear()
    update_folders_label()


def size_text(size_kb):
    if size_kb is None:
        return ""
    if size_kb >= 1024 * 1024:
        return f"{size_kb / (1024 * 1024):.1f} GB"
    if size_kb >= 1024:
        return f"{size_kb / 1024:.1f} MB"
    return f"{size_kb:.1f} KB"


def selected_path():
    selection = table.selection()
    if len(selection) == 0:
        return ""
    values = table.item(selection[0], "values")
    return os.path.join(values[1], values[0])


def set_busy(busy):
    global scanning
    scanning = busy
    if busy:
        search_button.config(text="Stop")
        force_stop_button.config(state="normal")
        state = "disabled"
    else:
        search_button.config(text="Search")
        force_stop_button.config(state="disabled")
        state = "normal"
    for button in (open_button, folder_button, csv_button, history_button, add_folder_btn, clear_folder_btn):
        button.config(state=state)


def force_stop():
    global stop_requested, force_stopped
    if scanning:
        stop_requested = True
        force_stopped = True


def show_progress(checked, found, folder):
    status.set(f"Searching...   {checked:,} items checked,   {found:,} found        {folder[:70]}")
    root.update()
    return not stop_requested


def clear_screen():
    global current_search, current_words
    current_search = 0
    current_words = []
    for row in table.get_children():
        table.delete(row)
    type_box.config(values=["All (0)"])
    type_choice.set("All (0)")


def fill_type_filter():
    total = 0
    choices = []
    for item_type, number in _db_count_by_type(con, current_search):
        choices.append(f"{item_type} ({number})")
        total = total + number
    choices.insert(0, f"All ({total})")
    type_box.config(values=choices)
    type_choice.set(choices[0])


def show_results(event=None):
    item_type = type_choice.get().split(" (")[0]
    rows = _db_get_results(con, current_search, item_type)
    for row in table.get_children():
        table.delete(row)
    number = 0
    for name, folder, kind, size_kb, matched in rows:
        dots = "●" * matched + "○" * (len(current_words) - matched)
        stripe = "even" if number % 2 == 0 else "odd"
        table.insert("", "end", values=(name, folder, kind, size_text(size_kb), dots), tags=(stripe,))
        number = number + 1


def start_search(event=None):
    global current_search, current_words, stop_requested, force_stopped
    if scanning:
        stop_requested = True
        return

    query = query_box.get().strip()
    keywords, ignored = _main_words(query, connector_words, split_var.get())
    if len(keywords) == 0:
        status.set("Type what you are looking for, e.g.   how many apple trees in california")
        return
    paths = chosen_paths()
    if len(paths) == 0:
        messagebox.showinfo("FileFinder", "Tick at least one drive or add a custom folder to search.")
        return

    words_text.set("Main words:   " + "  ·  ".join(keywords))
    if len(ignored) > 0:
        ignored_text.set("ignored: " + ", ".join(ignored))
    else:
        ignored_text.set("")
    if history_window is not None and history_window.winfo_exists():
        history_window.destroy()
    clear_screen()
    stop_requested = False
    force_stopped = False
    set_busy(True)
    search_id = 0
    try:
        search_id = _db_save_search(con, query, paths, keywords)
        matches, checked, total, stopped = _search(paths, keywords, skip_var.get(), show_progress)
        if force_stopped:
            _db_delete_search(con, search_id)
            clear_screen()
            status.set("Search force stopped.")
            return
        _db_save_results(con, search_id, matches, total)
    except Exception as error:
        status.set("Something went wrong.")
        messagebox.showerror("FileFinder", "Something went wrong:\n" + str(error))
        return
    finally:
        set_busy(False)
    if close_requested:
        root.destroy()
        return

    current_search = search_id
    current_words = keywords
    fill_type_filter()
    show_results()
    message = f"Found {total:,} in {checked:,} items checked"
    if stopped:
        message = "Stopped.  " + message
    if total > len(matches):
        message = message + f"   ·   showing the best {len(matches):,}"
    status.set(message + f"   ·   saved in MySQL as search #{search_id}")
    _save_settings({
        "drives": chosen_drives(),
        "folders": custom_folders,
        "skip_system": skip_var.get(),
        "split_words": split_var.get(),
    })


def load_search(search_id):
    global current_search, current_words
    query, drives, total, date = _db_get_search(con, search_id)
    current_search = search_id
    current_words = _db_get_keywords(con, search_id)
    query_box.delete(0, "end")
    query_box.insert(0, query)
    words_text.set("Main words:   " + "  ·  ".join(current_words))
    ignored_text.set("")
    fill_type_filter()
    show_results()
    status.set(f"Search #{search_id} of {date} ({drives}), read back from MySQL - the drives were not searched again")


def open_selected(event=None):
    path = selected_path()
    if path == "":
        return
    if not os.path.exists(path):
        messagebox.showwarning("FileFinder", "This is no longer there:\n" + path)
        return
    os.startfile(path)


def show_in_folder():
    path = selected_path()
    if path == "":
        return
    if not os.path.exists(path):
        messagebox.showwarning("FileFinder", "This is no longer there:\n" + path)
        return
    os.system('explorer /select,"' + path + '"')


def copy_path():
    path = selected_path()
    if path != "":
        root.clipboard_clear()
        root.clipboard_append(path)
        status.set("Copied:  " + path)


def save_csv():
    if current_search == 0:
        messagebox.showinfo("FileFinder", "Search for something first.")
        return
    file_name = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="results.csv",
                                             filetypes=[("CSV file", "*.csv")])
    if file_name == "":
        return
    item_type = type_choice.get().split(" (")[0]
    rows = _db_get_results(con, current_search, item_type)
    try:
        count = _export_csv(file_name, rows)
    except OSError as error:
        messagebox.showerror("FileFinder", "Could not save the file:\n" + str(error))
        return
    status.set(f"Saved {count} rows to {file_name}")


def show_menu(event):
    row = table.identify_row(event.y)
    if row != "":
        table.selection_set(row)
        menu.tk_popup(event.x_root, event.y_root)


def open_history():
    global history_window, history_table
    if history_window is not None and history_window.winfo_exists():
        history_window.lift()
        return
    history_window = tk.Toplevel(root)
    history_window.title("History - every search saved in MySQL")
    history_window.geometry("860x440")
    history_window.configure(bg=BG)

    frame = ttk.Frame(history_window, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Search history", style="Heading.TLabel").pack(anchor="w")
    ttk.Label(frame, textvariable=top_words_text, style="Sub.TLabel").pack(anchor="w", pady=(2, 10))

    buttons = ttk.Frame(frame, padding=(0, 10, 0, 0))
    buttons.pack(side="bottom", fill="x")
    ttk.Button(buttons, text="Show results", style="Accent.TButton", command=history_show).pack(side="left")
    ttk.Button(buttons, text="Delete", command=history_delete).pack(side="left", padx=8)
    ttk.Button(buttons, text="Clear all", command=history_clear).pack(side="left")
    ttk.Button(buttons, text="Close", command=history_window.destroy).pack(side="right")

    columns = ("id", "query", "words", "drives", "found", "date")
    history_table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=8)
    headings = ["#", "You typed", "Main words", "Drives", "Found", "Date"]
    widths   = [40,  280,         220,          90,       70,      90]
    for i in range(len(columns)):
        history_table.heading(columns[i], text=headings[i], anchor="w")
        history_table.column(columns[i], width=widths[i], anchor="w")
    history_table.pack(fill="both", expand=True)
    history_table.bind("<Double-1>", history_show)
    refresh_history()


def refresh_history():
    for row in history_table.get_children():
        history_table.delete(row)
    for search_id, query, keywords, drives, found, date in _db_get_history(con):
        history_table.insert("", "end", values=(search_id, query, ", ".join(keywords), drives, found, date))
    favourites = []
    for word, times in _db_top_words(con):
        favourites.append(f"{word} ({times})")
    if len(favourites) > 0:
        top_words_text.set("Words you search most:   " + ",  ".join(favourites))
    else:
        top_words_text.set("Double-click a search to see its results again - straight from MySQL, without searching the drives.")


def history_selected_id():
    selection = history_table.selection()
    if len(selection) == 0:
        return 0
    return int(history_table.item(selection[0], "values")[0])


def history_show(event=None):
    search_id = history_selected_id()
    if search_id != 0:
        load_search(search_id)
        history_window.destroy()


def history_delete():
    search_id = history_selected_id()
    if search_id == 0:
        return
    deleted = _db_delete_search(con, search_id)
    if search_id == current_search:
        clear_screen()
    status.set(f"Deleted search #{search_id} and its {deleted} results from MySQL")
    refresh_history()


def history_clear():
    if messagebox.askyesno("FileFinder", "Delete every saved search and result?", parent=history_window):
        _db_clear_history(con)
        clear_screen()
        status.set("History cleared")
        refresh_history()


def on_close():
    global stop_requested, close_requested
    if scanning:
        stop_requested = True
        close_requested = True
    else:
        root.destroy()


def open_database():
    global con, connector_words
    try:
        connector_words = _load_connector_words_from_text(_CONNECTOR_WORDS_TEXT)
        con = _db_connect()
    except (RuntimeError, OSError) as error:
        messagebox.showerror("FileFinder", str(error))
        return False
    return True


root = tk.Tk()
root.title("FileFinder")
root.geometry("1060x700")
root.minsize(780, 520)
root.configure(bg=BG)
root.protocol("WM_DELETE_WINDOW", on_close)

style = ttk.Style()
style.theme_use("clam")
style.configure(".",               background=BG,       foreground=DARK, font=(FONT, 10))
style.configure("Title.TLabel",   font=(FONT, 20, "bold"))
style.configure("Heading.TLabel", font=(FONT, 14, "bold"))
style.configure("Sub.TLabel",     foreground=GREY)
style.configure("Words.TLabel",   foreground=ACCENT, font=(FONT, 10, "bold"))
style.configure("TButton",        background="#e5e7eb", borderwidth=0, padding=(14, 7))
style.map("TButton",        background=[("disabled", "#eef0f3"), ("active", "#d1d5db")])
style.configure("Accent.TButton", background=ACCENT, foreground=WHITE, font=(FONT, 10, "bold"), padding=(22, 8))
style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")])
style.configure("TCheckbutton",   background=BG)
style.map("TCheckbutton",   background=[("active", BG)])
style.configure("TEntry",         fieldbackground=WHITE, bordercolor="#d1d5db", lightcolor="#d1d5db", padding=(10, 6))
style.configure("Treeview",       background=WHITE, fieldbackground=WHITE, rowheight=28, borderwidth=0)
style.configure("Treeview.Heading", background="#eceef2", font=(FONT, 10, "bold"), relief="flat", padding=(8, 6))
style.map("Treeview",       background=[("selected", "#dbeafe")], foreground=[("selected", DARK)])

settings       = _load_settings()
custom_folders = list(settings["folders"])
words_text     = tk.StringVar(value="Example:   how many apple trees in california")
ignored_text   = tk.StringVar(value="")
folders_text   = tk.StringVar(value="")
status         = tk.StringVar(value="Type what you are looking for, tick drives or add folders, and press Search.")
type_choice    = tk.StringVar(value="All (0)")
skip_var       = tk.BooleanVar(value=settings["skip_system"])
split_var      = tk.BooleanVar(value=settings["split_words"])
top_words_text = tk.StringVar(value="")

header = ttk.Frame(root, padding=(22, 16, 22, 2))
header.pack(fill="x")
ttk.Label(header, text="FileFinder", style="Title.TLabel").pack(side="left")
ttk.Label(header, text="   find any file or folder on your drives by its name",
          style="Sub.TLabel").pack(side="left", pady=(10, 0))

bar = ttk.Frame(root, padding=(22, 8, 22, 4))
bar.pack(fill="x")
query_box = ttk.Entry(bar, font=(FONT, 13))
query_box.pack(side="left", fill="x", expand=True)
query_box.bind("<Return>", start_search)
query_box.focus()
search_button = ttk.Button(bar, text="Search", style="Accent.TButton", width=9, command=start_search)
search_button.pack(side="left", padx=(10, 0))
force_stop_button = ttk.Button(bar, text="Force Stop", width=10, command=force_stop, state="disabled")
force_stop_button.pack(side="left", padx=(8, 0))

drives_row = ttk.Frame(root, padding=(22, 4))
drives_row.pack(fill="x")
ttk.Label(drives_row, text="Look in:", style="Sub.TLabel").pack(side="left")
drive_vars = {}
for drive in _list_drives():
    ticked = drive in settings["drives"] or (len(settings["drives"]) == 0 and len(custom_folders) == 0)
    drive_vars[drive] = tk.BooleanVar(value=ticked)
    ttk.Checkbutton(drives_row, text=drive[:2], variable=drive_vars[drive]).pack(side="left", padx=(12, 0))
ttk.Checkbutton(drives_row, text="Skip system folders",
                variable=skip_var).pack(side="right")
ttk.Checkbutton(drives_row, text="Split words",
                variable=split_var).pack(side="right", padx=(0, 16))

folders_row = ttk.Frame(root, padding=(22, 2, 22, 4))
folders_row.pack(fill="x")
add_folder_btn = ttk.Button(folders_row, text="Add folder...", command=add_custom_folder)
add_folder_btn.pack(side="left")
clear_folder_btn = ttk.Button(folders_row, text="Clear folders", command=clear_custom_folders)
clear_folder_btn.pack(side="left", padx=(8, 12))
ttk.Label(folders_row, textvariable=folders_text, style="Sub.TLabel").pack(side="left")
update_folders_label()

words_row = ttk.Frame(root, padding=(22, 6, 22, 2))
words_row.pack(fill="x")
ttk.Label(words_row, textvariable=words_text, style="Words.TLabel").pack(side="left")
ttk.Label(words_row, textvariable=ignored_text, style="Sub.TLabel").pack(side="left", padx=14)
type_box = ttk.Combobox(words_row, textvariable=type_choice, values=["All (0)"], state="readonly", width=16)
type_box.pack(side="right")
type_box.bind("<<ComboboxSelected>>", show_results)
ttk.Label(words_row, text="Show ", style="Sub.TLabel").pack(side="right")

ttk.Label(root, textvariable=status, style="Sub.TLabel", padding=(22, 0, 22, 12)).pack(side="bottom", fill="x")
bottom = ttk.Frame(root, padding=(22, 2, 22, 6))
bottom.pack(side="bottom", fill="x")
open_button    = ttk.Button(bottom, text="Open",           command=open_selected)
open_button.pack(side="left")
folder_button  = ttk.Button(bottom, text="Show in folder", command=show_in_folder)
folder_button.pack(side="left", padx=8)
csv_button     = ttk.Button(bottom, text="Save as CSV",    command=save_csv)
csv_button.pack(side="left")
history_button = ttk.Button(bottom, text="History",        command=open_history)
history_button.pack(side="right")

box = ttk.Frame(root, padding=(22, 6, 22, 6))
box.pack(fill="both", expand=True)
columns = ("name", "folder", "type", "size", "match")
table   = ttk.Treeview(box, columns=columns, show="headings", selectmode="browse")
headings = ["Name", "Location (folder)", "Type", "Size", "Match"]
widths   = [300,    440,                 90,     90,     70]
for i in range(len(columns)):
    table.heading(columns[i], text=headings[i], anchor="w")
    table.column(columns[i],  width=widths[i],  anchor="w")
scrollbar = ttk.Scrollbar(box, orient="vertical", command=table.yview)
table.configure(yscrollcommand=scrollbar.set)
scrollbar.pack(side="right", fill="y")
table.pack(side="left", fill="both", expand=True)
table.tag_configure("odd", background="#f8f9fb")
table.bind("<Double-1>", open_selected)
table.bind("<Return>",   open_selected)
table.bind("<Button-3>", show_menu)

menu = tk.Menu(root, tearoff=0)
menu.add_command(label="Open",           command=open_selected)
menu.add_command(label="Show in folder", command=show_in_folder)
menu.add_command(label="Copy full path", command=copy_path)

if __name__ == "__main__":
    if open_database():
        root.mainloop()
    else:
        root.destroy()