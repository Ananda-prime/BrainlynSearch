"""
Brainlynsearch — User Management CLI
=================================
Usage:
  python manage_users.py list
  python manage_users.py delete <email>
  python manage_users.py approve <email>
  python manage_users.py info <email>
  python manage_users.py import-db <source_db_path>
"""

import sqlite3
import sys
import os

# Resolve the database path robustly — search multiple possible locations
def _find_db() -> str:
    candidates = [
        # 1. Relative to this script file (primary)
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "api", "users.db"),
        # 2. Relative to current working directory
        os.path.join(os.getcwd(), "api", "users.db"),
        # 3. Script's parent/api (in case script is run from inside api/)
        os.path.join(os.getcwd(), "users.db"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    # Return primary path even if not found (get_conn will handle error)
    return candidates[0]

DB_PATH = _find_db()


def get_conn():
    print(f"[DB] Using: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at: {DB_PATH}")
        print("        Start the API server once to auto-create it.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list():
    """List all registered users."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, email, full_name, is_approved, role, tenant_id, created_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        print("No users registered yet.")
        return

    print(f"\n{'ID':<5} {'Email':<30} {'Name':<15} {'Approved':<10} {'Role':<12} {'Tenant ID':<15} {'Created At'}")
    print("-" * 115)
    for r in rows:
        approved = "YES" if r["is_approved"] else "NO"
        role = r["role"] if r["role"] else "individual"
        tenant_id = r["tenant_id"] if r["tenant_id"] else "N/A"
        print(f"{r['id']:<5} {r['email']:<30} {str(r['full_name']):<15} {approved:<10} {role:<12} {tenant_id:<15} {r['created_at']}")
    print(f"\nTotal: {len(rows)} user(s)\n")


def cmd_delete(email: str):
    """Delete a specific user by email."""
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email.lower(),)).fetchone()
    if not existing:
        print(f"[NOT FOUND] No user with email: {email}")
        conn.close()
        return

    conn.execute("DELETE FROM users WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()
    print(f"[DELETED] User '{email}' removed successfully.")


def cmd_approve(email: str):
    """Manually approve a user (skip OTP verification)."""
    conn = get_conn()
    existing = conn.execute("SELECT id, is_approved FROM users WHERE email = ?", (email.lower(),)).fetchone()
    if not existing:
        print(f"[NOT FOUND] No user with email: {email}")
        conn.close()
        return

    if existing["is_approved"]:
        print(f"[ALREADY APPROVED] '{email}' is already approved.")
        conn.close()
        return

    conn.execute("UPDATE users SET is_approved = 1 WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()
    print(f"[APPROVED] User '{email}' has been approved and can now log in.")


def cmd_info(email: str):
    """Show full details of a specific user."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, email, full_name, is_approved, role, tenant_id, organization_name, organization_domain, created_at FROM users WHERE email = ?",
        (email.lower(),)
    ).fetchone()
    conn.close()

    if not row:
        print(f"[NOT FOUND] No user with email: {email}")
        return

    print(f"\n--- User Info ---")
    print(f"  ID         : {row['id']}")
    print(f"  Email      : {row['email']}")
    print(f"  Name       : {row['full_name']}")
    print(f"  Approved   : {'YES' if row['is_approved'] else 'NO'}")
    print(f"  Role       : {row['role']}")
    print(f"  Tenant ID  : {row['tenant_id'] or 'N/A'}")
    print(f"  Org Name   : {row['organization_name'] or 'N/A'}")
    print(f"  Org Domain : {row['organization_domain'] or 'N/A'}")
    print(f"  Created    : {row['created_at']}\n")


def cmd_import_db(src_path: str):
    """Import and merge users from another users.db SQLite file."""
    if not os.path.exists(src_path):
        print(f"[ERROR] Source database file not found at: {src_path}")
        sys.exit(1)

    print(f"[IMPORT] Reading from source database: {src_path}")
    src_conn = sqlite3.connect(src_path)
    src_conn.row_factory = sqlite3.Row
    try:
        src_users = src_conn.execute(
            "SELECT email, password_hash, full_name, is_approved, created_at FROM users"
        ).fetchall()
    except Exception as e:
        print(f"[ERROR] Failed to read from source users table: {e}")
        src_conn.close()
        sys.exit(1)
    src_conn.close()

    if not src_users:
        print("[INFO] No users found in the source database.")
        return

    dest_conn = get_conn()
    inserted_count = 0
    updated_count = 0

    try:
        for u in src_users:
            email = u["email"].lower().strip()
            pwd_hash = u["password_hash"]
            name = u["full_name"]
            approved = u["is_approved"]
            created = u["created_at"]

            # Check if user already exists
            existing = dest_conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()

            if existing:
                dest_conn.execute(
                    """
                    UPDATE users 
                    SET password_hash = ?, full_name = ?, is_approved = ?, created_at = ?
                    WHERE email = ?
                    """,
                    (pwd_hash, name, approved, created, email)
                )
                updated_count += 1
            else:
                dest_conn.execute(
                    """
                    INSERT INTO users (email, password_hash, full_name, is_approved, created_at, role, tenant_id, organization_name, organization_domain)
                    VALUES (?, ?, ?, ?, ?, 'individual', NULL, NULL, NULL)
                    """,
                    (email, pwd_hash, name, approved, created)
                )
                inserted_count += 1
        dest_conn.commit()
    except Exception as e:
        dest_conn.rollback()
        print(f"[ERROR] Failed to merge databases: {e}")
        dest_conn.close()
        sys.exit(1)

    dest_conn.close()
    print(f"\n[SUCCESS] Import complete:")
    print(f"  - Users imported/inserted: {inserted_count}")
    print(f"  - Users updated: {updated_count}")
    print(f"  - Total processed: {len(src_users)}\n")


def cmd_set_role(email: str, role: str):
    """Set role for a specific user (super_admin, org_admin, org_user, individual)."""
    valid_roles = ["super_admin", "org_admin", "org_user", "individual"]
    role = role.lower().strip()
    if role not in valid_roles:
        print(f"[ERROR] Invalid role '{role}'. Valid options are: {', '.join(valid_roles)}")
        return

    conn = get_conn()
    existing = conn.execute("SELECT id, role FROM users WHERE email = ?", (email.lower(),)).fetchone()
    if not existing:
        print(f"[NOT FOUND] No user with email: {email}")
        conn.close()
        return

    conn.execute("UPDATE users SET role = ? WHERE email = ?", (role, email.lower()))
    conn.commit()
    conn.close()
    print(f"[ROLE UPDATED] User '{email}' role updated to '{role}'.")


def cmd_make_superadmin(email: str):
    """Promote a user to super_admin and approve them."""
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email.lower(),)).fetchone()
    if not existing:
        print(f"[NOT FOUND] No user with email: {email}")
        conn.close()
        return

    conn.execute("UPDATE users SET role = 'super_admin', is_approved = 1 WHERE email = ?", (email.lower(),))
    conn.commit()
    conn.close()
    print(f"[SUPER ADMIN] User '{email}' has been promoted to super_admin and approved.")


def cmd_add_user(email: str, password: str, name: str = "Test User", role: str = "individual"):
    """Add a new user directly into the database without OTP verification."""
    email = email.lower().strip()
    role = role.lower().strip()
    valid_roles = ["super_admin", "org_admin", "org_user", "individual"]
    if role not in valid_roles:
        print(f"[ERROR] Invalid role '{role}'. Valid options are: {', '.join(valid_roles)}")
        return

    import bcrypt
    pwd_bytes = password.encode('utf-8')
    pwd_hash = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode('utf-8')

    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        print(f"[EXISTS] User '{email}' already exists. Updating password, name & role, approving account...")
        conn.execute(
            "UPDATE users SET password_hash = ?, full_name = ?, role = ?, is_approved = 1 WHERE email = ?",
            (pwd_hash, name, role, email)
        )
    else:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, full_name, is_approved, role, tenant_id, organization_name, organization_domain)
            VALUES (?, ?, ?, 1, ?, NULL, NULL, NULL);
            """,
            (email, pwd_hash, name, role)
        )
        print(f"[CREATED] User '{email}' created successfully (Approved=YES, Role='{role}').")

    conn.commit()
    conn.close()


def print_usage():
    print(__doc__)


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print_usage()
        sys.exit(0)

    command = args[0].lower()

    if command == "list":
        cmd_list()

    elif command == "delete":
        if len(args) < 2:
            print("[ERROR] Usage: python manage_users.py delete <email>")
            sys.exit(1)
        cmd_delete(args[1])

    elif command == "approve":
        if len(args) < 2:
            print("[ERROR] Usage: python manage_users.py approve <email>")
            sys.exit(1)
        cmd_approve(args[1])

    elif command == "info":
        if len(args) < 2:
            print("[ERROR] Usage: python manage_users.py info <email>")
            sys.exit(1)
        cmd_info(args[1])

    elif command == "set-role":
        if len(args) < 3:
            print("[ERROR] Usage: python manage_users.py set-role <email> <role>")
            sys.exit(1)
        cmd_set_role(args[1], args[2])

    elif command == "make-superadmin":
        if len(args) < 2:
            print("[ERROR] Usage: python manage_users.py make-superadmin <email>")
            sys.exit(1)
        cmd_make_superadmin(args[1])

    elif command in ("add-user", "create-user"):
        if len(args) < 3:
            print("[ERROR] Usage: python manage_users.py add-user <email> <password> [full_name] [role]")
            sys.exit(1)
        name = args[3] if len(args) > 3 else "Test User"
        role = args[4] if len(args) > 4 else "individual"
        cmd_add_user(args[1], args[2], name, role)

    elif command == "import-db":
        if len(args) < 2:
            print("[ERROR] Usage: python manage_users.py import-db <source_db_path>")
            sys.exit(1)
        cmd_import_db(args[1])

    else:
        print(f"[ERROR] Unknown command: '{command}'")
        print_usage()
        sys.exit(1)


