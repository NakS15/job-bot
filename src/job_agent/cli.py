import argparse
import asyncio


def main() -> None:
    p = argparse.ArgumentParser(prog="job-agent")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server (stdio)")
    sub.add_parser("login", help="sign in to LinkedIn in the apply browser profile")
    r = sub.add_parser("import-resume", help="store base resume PDF")
    r.add_argument("pdf")
    sub.add_parser("sheet", help="rebuild the spreadsheet")
    a = sub.add_parser("apply", help="apply to rows marked YES in the spreadsheet")
    a.add_argument("--live", action="store_true", help="actually submit (default is dry run)")
    ae = sub.add_parser("apply-external", help="walk approved external applications page by page (dry run unless --live)")
    ae.add_argument("--live", action="store_true", help="actually click the final Submit")
    ae.add_argument("--no-wait", action="store_true", help="do not pause for you at the end; close the browser")
    ae.add_argument("--only", help="job id to process (default: all approved external jobs)")
    sp = sub.add_parser("set-password", help="store a portal password in Keychain (hidden prompt)")
    sp.add_argument("domain", help="portal host, e.g. citi.wd5.myworkdayjobs.com")
    args = p.parse_args()

    if args.cmd in (None, "serve"):
        from .server import serve
        serve()
    elif args.cmd == "login":
        from .applier import run_login
        asyncio.run(run_login())
    elif args.cmd == "import-resume":
        from .resume import import_resume
        print(import_resume(args.pdf)[:500])
    elif args.cmd == "sheet":
        from .sheet import build_sheet
        print(build_sheet())
    elif args.cmd == "apply":
        from .applier import apply_approved
        for row in asyncio.run(apply_approved(dry_run=not args.live)):
            print(row)
    elif args.cmd == "set-password":
        import getpass

        from . import credentials
        from .config import load_profile
        email = load_profile()["answers"]["email"]
        pw = getpass.getpass(f"Password for {email} on {args.domain} (hidden): ")
        credentials.save(args.domain, email, pw)
        credentials.set_state(args.domain, email, "created")
        print(f"Saved to Keychain for {args.domain}; the tool will now sign in with it.")
    elif args.cmd == "apply-external":
        from .applier import apply_external
        for row in asyncio.run(apply_external(dry_run=not args.live, wait=not args.no_wait, only=args.only)):
            print(row)


if __name__ == "__main__":
    main()
