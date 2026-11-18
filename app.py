"""Executable entry: GUI without arguments; CLI and isolated self-tests otherwise."""
from pathlib import Path
import contextlib
import io
import json
import sys
import desktop
import desktop_service
import migrate


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        desktop.launch()
        return 0
    report = None
    if '--report' in args:
        index = args.index('--report')
        if index + 1 >= len(args):
            return 2
        report = Path(args[index + 1]).absolute()
        del args[index:index + 2]
    output = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if args == ['--version']:
                print(desktop_service.VERSION)
                code = 0
            elif args == ['--smoke-gui']:
                print(json.dumps(desktop.smoke_test(), ensure_ascii=False, indent=2))
                code = 0
            elif args == ['--self-test']:
                from self_test import run
                print(json.dumps(run(), ensure_ascii=False, indent=2))
                code = 0
            else:
                try:
                    code = migrate.main(args)
                except SystemExit as exc:
                    code = exc.code or 0
    except Exception as exc:
        error = str(exc)
        output.write(json.dumps({'error': error}, ensure_ascii=False))
        code = 2
    text = output.getvalue()
    if report:
        # Callers choose a report file; no implicit logging of private paths/data.
        with report.open('x', encoding='utf-8') as writer:
            writer.write(text)
    if sys.stdout is not None:
        sys.stdout.write(text)
    elif code and error and not report:
        from tkinter import messagebox
        messagebox.showerror('操作未完成', error)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
