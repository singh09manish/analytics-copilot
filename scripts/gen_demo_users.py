"""Generate bcrypt hashes for the two demo accounts. Paste output into .env."""
import getpass

import bcrypt


def main() -> None:
    for account in ("analyst", "admin"):
        pw = getpass.getpass(f"Password for {account}@demo: ")
        h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
        print(f"DEMO_{account.upper()}_PASSWORD_HASH={h}")
    print("JWT_SECRET=" + bcrypt.gensalt().hex())


if __name__ == "__main__":
    main()
