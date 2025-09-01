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
    try:
        return pymysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
    except pymysql.MySQLError as e:
        print(f"❌ MySQL connection error: {e}")
        return None

def get_username_from_db(discord_id):
    conn = get_database_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name FROM users WHERE discord_id=%s", (discord_id,))
            row = cursor.fetchone()
            return row['name'] if row else None
    finally:
        conn.close()

def get_email_from_db(discord_id):
    conn = get_database_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT email FROM users WHERE discord_id=%s", (discord_id,))
            row = cursor.fetchone()
            return row['email'] if row else None
    finally:
        conn.close()

def get_stored_roles(member_id):
    conn = get_database_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT r.name FROM roles r
                JOIN user_roles ur ON r.id = ur.role_id
                JOIN users u ON u.id = ur.user_id
                WHERE u.discord_id=%s
            """, (member_id,))
            rows = cursor.fetchall()
            return [row['name'] for row in rows] if rows else []
    finally:
        conn.close()

def store_user_roles(member):
    db_conn = get_database_connection()
    if not db_conn:
        return
    try:
        current_roles = {role.name: role.id for role in member.roles if not role.is_default()}
        db_roles = set(get_stored_roles(member.id))

        with db_conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE discord_id=%s", (member.id,))
            user_row = cursor.fetchone()
            if not user_row:
                print(f"⚠ User with Discord ID {member.id} not found in database. Ignoring role sync.")
                return
            user_id = user_row['id']

            # Add or update roles
            for role_name, role_id_value in current_roles.items():
                if role_name not in db_roles:
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
            # Remove old roles
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

# ---------------- LDAP Helpers ----------------
def get_ldap_connection():
    try:
        server = Server(LDAP_SERVER, get_info=ALL)
        conn = Connection(server, LDAP_USER, LDAP_PASSWORD, auto_bind=True)
        return conn
    except (LDAPBindError, LDAPSocketOpenError, LDAPException) as e:
        print(f"❌ LDAP connection error: {e}")
        return None

def ensure_ou_structure(conn):
    conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=users))", attributes=["ou"])
    if not conn.entries:
        conn.add(USER_OU_DN, ["organizationalUnit", "top"], {"ou": "users"})
    conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=groups))", attributes=["ou"])
    if not conn.entries:
        conn.add(GROUP_OU_DN, ["organizationalUnit", "top"], {"ou": "groups"})

def ensure_user_in_ldap(conn, member):
    db_username = get_username_from_db(member.id)
    if not db_username:
        db_username = member.name
    db_email = get_email_from_db(member.id) or f"{db_username}@example.com"

    user_dn = f"uid={db_username},{USER_OU_DN}"
    conn.search(USER_OU_DN, f"(uid={db_username})", attributes=["uid"])
    if not conn.entries:
        conn.add(user_dn, [
            'inetOrgPerson', 'organizationalPerson', 'chrimeraPerson', 'person', 'top'
        ], {
            'cn': db_username,
            'sn': db_username,
            'givenName': db_username,
            'uid': db_username,
            'mail': db_email
        })

def ensure_nobody_user(conn):
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
    group_dn = f"cn={role_name},{GROUP_OU_DN}"
    conn.search(GROUP_OU_DN, f"(cn={role_name})", attributes=["member"])
    if not conn.entries:
        conn.add(group_dn, ['groupOfNames', 'top'], {'cn': role_name, 'member': [NOBODY_DN]})
    else:
        members = set(str(m) for m in getattr(conn.entries[0], "member", []))
        if NOBODY_DN not in members:
            conn.modify(group_dn, {'member': [(MODIFY_ADD, [NOBODY_DN])]})

def add_user_to_group(conn, user_dn, role_name):
    ensure_group_in_ldap(conn, role_name)
    conn.modify(f"cn={role_name},{GROUP_OU_DN}", {'member': [(MODIFY_ADD, [user_dn])]})

def remove_user_from_group(conn, user_dn, role_name):
    group_dn = f"cn={role_name},{GROUP_OU_DN}"
    conn.modify(group_dn, {'member': [(MODIFY_DELETE, [user_dn])]})

def rename_ldap_group(conn, old_name, new_name):
    old_dn = f"cn={old_name},{GROUP_OU_DN}"
    try:
        conn.modify_dn(old_dn, f"cn={new_name}")
        print(f"✅ LDAP: Renamed group '{old_name}' → '{new_name}'")
    except LDAPException as e:
        print(f"❌ LDAP rename error: {e}")

# ---------------- System Helpers ----------------
def trigger_php_hook(discord_id, old_role, new_role):
    php_script = os.path.expanduser(ROLE_HOOK)
    try:
        result = subprocess.run(
            ["php", php_script, str(discord_id), old_role, new_role],
            capture_output=True,
            text=True,
            check=True
        )
        print(f"✅ PHP hook executed: {result.stdout.strip()}")
    except Exception as e:
        print(f"❌ PHP hook error: {e}")

# ---------------- Core Update Function ----------------
def update_user_groups(member):
    conn = get_ldap_connection()
    if not conn:
        return
    try:
        ensure_user_in_ldap(conn, member)
        ensure_nobody_user(conn)

        db_username = get_username_from_db(member.id)
        if not db_username:
            db_username = member.name

        user_dn = f"uid={db_username},{USER_OU_DN}"

        stored_roles = set(get_stored_roles(member.id))
        current_roles = set(role.name for role in member.roles if not role.is_default())

        # Remove old roles
        for role_name in stored_roles - current_roles:
            print(f"[DEBUG] REMOVE {member.name} from group '{role_name}'")
            remove_user_from_group(conn, user_dn, role_name)
            trigger_php_hook(member.id, role_name, "")

        # Add new roles
        for role_name in current_roles - stored_roles:
            print(f"[DEBUG] ADD {member.name} to group '{role_name}'")
            add_user_to_group(conn, user_dn, role_name)
            trigger_php_hook(member.id, "", role_name)

        # Update database
        store_user_roles(member)
    finally:
        conn.unbind()

# ---------------- Discord Events ----------------
@bot.event
async def on_ready():
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

    # Sync roles on startup
    for member in my_guild.members:
        update_user_groups(member)

@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        update_user_groups(after)

@bot.event
async def on_member_join(member):
    update_user_groups(member)

@bot.event
async def on_guild_role_update(before, after):
    if before.name != after.name:
        update_role_name_in_db(after.id, after.name)
        conn = get_ldap_connection()
        if conn:
            rename_ldap_group(conn, before.name, after.name)
            conn.unbind()
        trigger_php_hook(0, before.name, after.name)

# ---------------- Main ----------------
if __name__ == "__main__":
    bot.run(TOKEN)
