#!/usr/bin/python3
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import pymysql.cursors
from ldap3 import Server, Connection, ALL, MODIFY_ADD, MODIFY_DELETE, MODIFY_REPLACE
from ldap3.core.exceptions import LDAPException, LDAPBindError, LDAPSocketOpenError
import subprocess

# ---------------- Environment ----------------
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = int(os.getenv('DISCORD_GUILD'))
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE')
LDAP_SERVER = os.getenv('LDAP_SERVER')
LDAP_USER = os.getenv('LDAP_USER')
LDAP_PASSWORD = os.getenv('LDAP_PASSWORD')
LDAP_BASE_DN = os.getenv('LDAP_BASE_DN', 'dc=yetanotherprojecttosavetheworld,dc=org')
USER_OU_DN = f"ou=users,{LDAP_BASE_DN}"
GROUP_OU_DN = f"ou=groups,{LDAP_BASE_DN}"
NOBODY_UID = "nobody_001"
NOBODY_DN = f"uid={NOBODY_UID},{USER_OU_DN}"

ROLE_HOOK = os.getenv('ROLE_HOOK')

# ---------------- Discord Bot ----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)
my_guild = None

# ---------------- MySQL Helpers ----------------
def get_database_connection():
    """Returns a connection to the MySQL database."""
    try:
        connection = pymysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
        return connection
    except pymysql.MySQLError as e:
        print(f"❌ MySQL connection error: {e}")
        return None

async def ensure_all_ldap_groups_from_discord(conn):
    """Create LDAP groups for every role in the Discord guild."""
    for role in my_guild.roles:
        if not role.is_default():  # skip @everyone
            ensure_group_in_ldap(conn, role.name)

async def sync_ldap_from_discord():
    """Sync all Discord roles and members into LDAP."""
    conn = get_ldap_connection()
    if not conn:
        return

    try:
        # Step 1: Create all groups
        for role in my_guild.roles:
            if not role.is_default():  # skip @everyone
                ensure_group_in_ldap(conn, role.name)

        # Step 2: Ensure all users exist in LDAP
        for member in my_guild.members:
            ensure_user_in_ldap(conn, member)
        ensure_nobody_user(conn)

        # Step 3: Add users to their groups
        for member in my_guild.members:
            user_dn = f"uid={member.id},{USER_OU_DN}"
            for role in member.roles:
                if not role.is_default():
                    add_user_to_group(conn, user_dn, role.name)
    finally:
        conn.unbind()



def store_user_roles(member):
    """
    Stores or updates a member's roles in the database.
    This function now also handles role name updates implicitly via ON DUPLICATE KEY.
    """
    db_conn = get_database_connection()
    if not db_conn:
        return
    try:
        current_roles = {role.name: role.id for role in member.roles if not role.is_default()}
        db_roles = set(get_stored_roles(member.id))  # fetch role names from DB

        # Check if the user exists in the database. If not, ignore them.
        with db_conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE discord_id=%s", (member.id,))
            user_row = cursor.fetchone()
            if not user_row:
                print(f"⚠️ User with Discord ID {member.id} not found in database. Ignoring role sync.")
                return  # Exit the function if user is not found.
            user_id = user_row['id']

            # Roles to add
            for role_name, role_id_value in current_roles.items():
                if role_name not in db_roles:
                    # Insert or update role. ON DUPLICATE KEY UPDATE handles role name changes.
                    cursor.execute(
                        "INSERT INTO roles (role_id, name) VALUES (%s, %s) ON DUPLICATE KEY UPDATE name=%s",
                        (role_id_value, role_name, role_name)
                    )
                    cursor.execute("SELECT id FROM roles WHERE role_id=%s", (role_id_value,))
                    role_row = cursor.fetchone()
                    if role_row:
                        cursor.execute(
                            "INSERT IGNORE INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                            (user_id, role_row['id'])
                        )

            # Roles to remove
            for role_name in db_roles - set(current_roles.keys()):
                cursor.execute("SELECT id FROM roles WHERE name=%s", (role_name,))
                role_row = cursor.fetchone()
                if role_row:
                    cursor.execute(
                        "DELETE FROM user_roles WHERE user_id=%s AND role_id=%s",
                        (user_id, role_row['id'])
                    )
    finally:
        db_conn.close()

def get_username_from_db(discord_id):
    """Fetches the 'name' field from the users table by Discord ID."""
    db_conn = get_database_connection()
    if not db_conn:
        return None
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("SELECT name FROM users WHERE discord_id=%s", (discord_id,))
            row = cursor.fetchone()
            return row['name'] if row else None
    finally:
        db_conn.close()

def get_email_from_db(discord_id):
    """Fetches the 'email' field from the users table by Discord ID."""
    db_conn = get_database_connection()
    if not db_conn:
        return None
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("SELECT email FROM users WHERE discord_id=%s", (discord_id,))
            row = cursor.fetchone()
            return row['email'] if row else None
    finally:
        db_conn.close()

def update_role_name_in_db(role_id, new_name):
    """Updates the name of a role in the database based on its ID."""
    db_conn = get_database_connection()
    if not db_conn:
        return
    try:
        with db_conn.cursor() as cursor:
            cursor.execute(
                "UPDATE roles SET name=%s WHERE role_id=%s",
                (new_name, role_id)
            )
            print(f"✅ MySQL: Role with ID {role_id} name updated to '{new_name}'")
    finally:
        db_conn.close()


def get_stored_roles(member_id):
    """Fetches role names from the database for a specific member."""
    db_conn = get_database_connection()
    if not db_conn:
        return []
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("""
                SELECT r.name FROM roles r
                JOIN user_roles ur ON r.id = ur.role_id
                JOIN users u ON u.id = ur.user_id
                WHERE u.discord_id=%s
            """, (member_id,))
            rows = cursor.fetchall()
            return [row['name'] for row in rows] if rows else []
    finally:
        db_conn.close()
        
def get_all_stored_roles():
    """Fetches all roles from the database and returns them as a dictionary mapping ID to name."""
    db_conn = get_database_connection()
    if not db_conn:
        return {}
    try:
        with db_conn.cursor() as cursor:
            cursor.execute("SELECT role_id, name FROM roles")
            rows = cursor.fetchall()
            return {row['role_id']: row['name'] for row in rows}
    finally:
        db_conn.close()

# ---------------- System Helpers ----------------
def trigger_php_hook(discord_id, old_role, new_role):
    """
    Triggers a PHP script to handle system-level changes.
    Passes old_role and new_role for context.
    """
    php_script = os.path.expanduser(ROLE_HOOK)
    try:
        result = subprocess.run(
            ["php", php_script, str(discord_id), old_role, new_role],
            capture_output=True,
            text=True,
            check=True
        )
        print(f"✅ PHP hook executed: {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"❌ PHP hook error ({e.returncode}): {e.stderr.strip()}")
    except Exception as e:
        print(f"❌ Unexpected PHP hook error: {e}")

# ---------------- LDAP Helpers ----------------
def get_ldap_connection():
    """Returns a connection to the LDAP server."""
    try:
        server = Server(LDAP_SERVER, get_info=ALL)
        conn = Connection(server, LDAP_USER, LDAP_PASSWORD, auto_bind=True)
        return conn
    except (LDAPBindError, LDAPSocketOpenError, LDAPException) as e:
        print(f"❌ LDAP connection error: {e}")
        return None

def ensure_ou_structure(conn):
    """Ensures the base OU structure exists."""
    conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=users))", attributes=["ou"])
    if not conn.entries:
        conn.add(USER_OU_DN, ["organizationalUnit", "top"], {"ou": "users"})
    conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=groups))", attributes=["ou"])
    if not conn.entries:
        conn.add(GROUP_OU_DN, ["organizationalUnit", "top"], {"ou": "groups"})

def ensure_user_in_ldap(conn, member):
    """Ensures a Discord member exists as a user in LDAP."""


    uid = str(member.id)
    user_dn = f"uid={uid},{USER_OU_DN}"
    conn.search(USER_OU_DN, f"(uid={uid})", attributes=["uid"])
    if not conn.entries:
      db_user = get_username_from_db(member.id)
      print(db_user) 
      if not db_user:
        db_user = member.name  # fallback if DB entry missing
      db_email = get_email_from_db(member.id)
      if not db_email:
         db_email = f"{member.name}@example.com"
      conn.add(user_dn, ['inetOrgPerson', 'organizationalPerson', 'chrimeraPerson','person', 'top'], {
            'cn': member.name,
            'sn': member.name,
            'givenName': db_user,
            'uid': uid,
            'mail': db_email
      })

def ensure_nobody_user(conn):
    """Ensures a placeholder 'nobody' user exists in LDAP."""
    conn.search(USER_OU_DN, f"(uid={NOBODY_UID})", attributes=["uid"])
    if not conn.entries:
        conn.add(NOBODY_DN, ['inetOrgPerson', 'organizationalPerson', 'person', 'top'], {
            'cn': 'Nobody',
            'sn': 'Placeholder',
            'givenName': 'Nobody',
            'uid': NOBODY_UID,
            'mail': 'nobody@example.com'
        })

def ensure_group_in_ldap(conn, role_name):
    """Ensures an LDAP group exists for the given role and contains a placeholder member."""
    group_dn = f"cn={role_name},{GROUP_OU_DN}"
    conn.search(GROUP_OU_DN, f"(cn={role_name})", attributes=["member"])
    if not conn.entries:
        conn.add(group_dn, ['groupOfNames', 'top'], {'cn': role_name, 'member': [NOBODY_DN]})
    else:
        members = set(str(m) for m in getattr(conn.entries[0], "member", []))
        if NOBODY_DN not in members:
            conn.modify(group_dn, {'member': [(MODIFY_ADD, [NOBODY_DN])]})

def add_user_to_group(conn, user_dn, role_name):
    """Adds a user to a group in LDAP."""
    ensure_group_in_ldap(conn, role_name)
    conn.modify(f"cn={role_name},{GROUP_OU_DN}", {'member': [(MODIFY_ADD, [user_dn])]})

def remove_user_from_group(conn, user_dn, role_name):
    """Removes a user from a group in LDAP."""
    group_dn = f"cn={role_name},{GROUP_OU_DN}"
    conn.modify(group_dn, {'member': [(MODIFY_DELETE, [user_dn])]})
    # If the group becomes empty (except for nobody), you could add a check here
    # to delete the group, but it's not required by the current logic.

def rename_ldap_group(conn, old_name, new_name):
    """Renames an existing LDAP group."""
    old_dn = f"cn={old_name},{GROUP_OU_DN}"
    new_rdn = f"cn={new_name}"
    try:
        # Use modify_dn instead of rename, as the installed ldap3 version may not support it.
        conn.modify_dn(old_dn, new_rdn)
        print(f"✅ LDAP: Renamed group '{old_name}' to '{new_name}'")
    except LDAPException as e:
        print(f"❌ LDAP rename error: {e}")

# ---------------- Core Update Function ----------------
def update_user_groups(member):
    """Sync LDAP and DB based on current roles."""
    conn = get_ldap_connection()
    if not conn:
        return
    try:
        # We ensure user exists in LDAP, even if not in DB, as requested by the user
        ensure_user_in_ldap(conn, member)
        ensure_nobody_user(conn)

        stored_roles = set(get_stored_roles(member.id))
        current_roles = set(role.name for role in member.roles if not role.is_default())
        
        # Roles to remove
        for role_name in stored_roles - current_roles:
            print(f"[DEBUG] REMOVE {member.name} from group '{role_name}'")
            remove_user_from_group(conn, f"uid={member.id},{USER_OU_DN}", role_name)
            trigger_php_hook(member.id, role_name, "")  # old_role -> removed
        
        # Roles to add
        for role_name in current_roles - stored_roles:
            print(f"[DEBUG] ADD {member.name} to group '{role_name}'")
            add_user_to_group(conn, f"uid={member.id},{USER_OU_DN}", role_name)
            trigger_php_hook(member.id, "", role_name)  # added -> new_role

        # Update DB
        store_user_roles(member)
    finally:
        conn.unbind()

def set_quota(discord_id):
    """
    Dynamically sets LDAP quota based on the highest quota among all user's roles in MySQL.
    """
    db_conn = get_database_connection()
    if not db_conn:
        return 0

    try:
        with db_conn.cursor() as cursor:
            # Fetch the highest quota for the user's roles
            cursor.execute("""
                SELECT MAX(r.quota) AS max_quota
                FROM roles r
                JOIN user_roles ur ON r.id = ur.role_id
                JOIN users u ON u.id = ur.user_id
                WHERE u.discord_id = %s
            """, (discord_id,))
            row = cursor.fetchone()
            quota_mb = row['max_quota'] if row and row['max_quota'] is not None else 0
    finally:
        db_conn.close()

    # Update LDAP
    conn = get_ldap_connection()
    if conn:
        user_dn = f"uid={discord_id},{USER_OU_DN}"
        try:
            quota_str = f"{quota_mb}MB"
            conn.modify(user_dn, {'quota': [(MODIFY_REPLACE, [quota_str])]})
            print(f"✅ LDAP quota updated for {discord_id} to {quota_mb}MB")
        except Exception as e:
            print(f"❌ Failed to set LDAP quota for {discord_id}: {e}")
        finally:
            conn.unbind()

    return quota_mb



# ---------------- Startup Sync ----------------
async def sync_roles_at_startup():
    """Iterates through all members and syncs their roles with LDAP and MySQL."""
    print("[DEBUG] Syncing all roles at startup...")
    for member in my_guild.members:
        update_user_groups(member)
        set_quota(member.id)

async def check_for_role_renames_on_startup():
    """Checks for role renames and updates them in MySQL and LDAP."""
    print("[DEBUG] Checking for role renames on startup...")
    conn = get_ldap_connection()
    if not conn:
        return
    try:
        discord_roles = {r.id: r.name for r in my_guild.roles if not r.is_default()}
        db_roles = get_all_stored_roles()

        for role_id, db_name in db_roles.items():
            if role_id in discord_roles:
                discord_name = discord_roles[role_id]
                if db_name != discord_name:
                    print(f"⚠️ Found role rename on startup: '{db_name}' -> '{discord_name}'")
                    update_role_name_in_db(role_id, discord_name)
                    rename_ldap_group(conn, db_name, discord_name)
                    # Trigger PHP hook for the rename event
                    trigger_php_hook(0, db_name, discord_name)
    finally:
        conn.unbind()
        
# ---------------- Discord Events ----------------
@bot.event
async def on_ready():
    """Initial setup when the bot connects."""
    global my_guild
    print(f'✅ {bot.user} connected!')
    for guild in bot.guilds:
        if guild.id == GUILD:
            my_guild = guild
            break
    if not my_guild:
        print("❌ Guild not found. Exiting.")
        return

    conn = get_ldap_connection()
    if conn:
        ensure_ou_structure(conn)
        ensure_nobody_user(conn)
        conn.unbind()

    await sync_ldap_from_discord()


@bot.event
async def on_guild_role_update(before, after):
    """
    This event handler detects when a role name changes in Discord
    and updates the corresponding group in MySQL and LDAP.
    """
    if before.name != after.name:
        print(f"[DEBUG] Role name changed: '{before.name}' -> '{after.name}'")
        # Update MySQL
        update_role_name_in_db(after.id, after.name)
        # Update LDAP
        conn = get_ldap_connection()
        if conn:
            rename_ldap_group(conn, before.name, after.name)
            conn.unbind()
        # Trigger PHP hook for the rename event
        trigger_php_hook(0, before.name, after.name)

@bot.event
async def on_member_join(member):
    """Handles role synchronization when a member joins the guild."""
    update_user_groups(member)

@bot.event
async def on_member_update(before, after):
    """Handles role synchronization when a member's roles change."""
    if before.roles != after.roles:
        update_user_groups(after)
    set_quota(after.id)

# ---------------- Main ----------------
if __name__ == "__main__":
    bot.run(TOKEN)

