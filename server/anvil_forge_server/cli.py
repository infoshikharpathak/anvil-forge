"""anvil-forge-server: serve, migrate, create-user."""

from __future__ import annotations

import argparse
import getpass
import sys

from .settings import load_settings


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("anvil_forge_server.app:create_app", factory=True, host=args.host, port=args.port,
                reload=args.reload, proxy_headers=True)
    return 0


def _migrate(_args: argparse.Namespace) -> int:
    from .db import migrate

    settings = load_settings()
    migrate(settings.database_url)
    print(f"Database is up to date ({_safe_url(settings.database_url)}).")
    return 0


def _create_user(args: argparse.Namespace) -> int:
    from .accounts import AccountError, create_user
    from .db import make_engine, make_sessionmaker, migrate

    settings = load_settings()
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords don't match.", file=sys.stderr)
            return 1
    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_sessionmaker(engine).begin() as db:
            user = create_user(db, args.email, password)
            email = user.email
    except AccountError as e:
        print(e.message, file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"Created {email}. Log in at /ui/login.")
    return 0


def _safe_url(url: str) -> str:
    """Hide a password in a database URL before printing it."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anvil-forge-server", description="anvil-forge control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the service (migrates the database first)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    serve.set_defaults(func=_serve)

    mig = sub.add_parser("migrate", help="upgrade the database schema to the latest version")
    mig.set_defaults(func=_migrate)

    cu = sub.add_parser("create-user", help="create an account (how the first user is made)")
    cu.add_argument("--email", required=True)
    cu.add_argument("--password-stdin", action="store_true",
                    help="read the password from stdin instead of prompting (for scripts)")
    cu.set_defaults(func=_create_user)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
