"""Windows-friendly local file picker; importing sources never imports credentials."""
from pathlib import Path
import sys

def main():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        file = filedialog.askopenfilename(parent=root, title="选择普通来源配置 JSON",
            initialfile="sources.json", filetypes=[("JSON 来源配置","*.json")])
        if not file:
            root.destroy()
            return 0
        from notices import main as notices_main
        result = notices_main(["sources","import",file])
        if result == 0:
            messagebox.showinfo("导入完成","来源已更新，旧配置已备份。请在本地控制台点击“重新载入磁盘配置”。",parent=root)
        else:
            messagebox.showerror("未导入","清单格式不正确或含敏感字段，原配置保留。请查看终端提示。",parent=root)
        root.destroy()
        return result
    except ImportError:
        print("缺少 Tk 或运行依赖。可先运行 setup.bat，也可使用 notices.py sources import 文件路径。")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
