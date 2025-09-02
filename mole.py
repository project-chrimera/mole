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

POSTFIX_MEMBER_GROUP = os.getenv('POSTFIX_MEMBER_GROUP', 'linux_users')
POSTFIX_ROOT_GROUP = os.getenv('POSTFIX_ROOT_GROUP', 'linux_admins')
POSTFIX_MEMBER_GID = int(os.getenv('POSTFIX_MEMBER_GID', '10000'))
POSTFIX_ROOT_GID   = int(os.getenv('POSTFIX_ROOT_GID', '10001'))

POSTFIX_MEMBER_ROLE_ID = int(os.getenv('POSTFIX_MEMBER_ROLE_ID', '1412179766466969661'))
POSTFIX_ROOT_ROLE_ID   = int(os.getenv('POSTFIX_ROOT_ROLE_ID', '1412179886277263460'))

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

def get_current_groups(conn, username):
    """
    Return a list of LDAP groups the given username belongs to.
    """
    groups = []

    try:
        conn.search(
            search_base=LDAP_GROUP_BASE,
            search_filter=f"(|(memberUid={username})(uniqueMember=uid={username},{LDAP_USER_BASE}))",
            attributes=["cn"]
        )

        for entry in conn.entries:
            groups.append(str(entry.cn))

        print(f"[DEBUG] Current LDAP groups for {username}: {groups}")

    except Exception as e:
        print(f"❌ Error fetching groups for {username}: {e}")

    return groups


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
    """Ensure the LDAP OUs exist, raise error if fails."""
    try:
        # Users OU
        conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=users))", attributes=["ou"])
        if not conn.entries:
            if not conn.add(USER_OU_DN, ["organizationalUnit", "top"], {"ou": "users"}):
                raise Exception(f"Failed to create OU: {USER_OU_DN}")
            print(f"✅ Created OU: {USER_OU_DN}")

        # Groups OU
        conn.search(LDAP_BASE_DN, f"(&(objectClass=organizationalUnit)(ou=groups))", attributes=["ou"])
        if not conn.entries:
            if not conn.add(GROUP_OU_DN, ["organizationalUnit", "top"], {"ou": "groups"}):
                raise Exception(f"Failed to create OU: {GROUP_OU_DN}")
            print(f"✅ Created OU: {GROUP_OU_DN}")

    except Exception as e:
        raise Exception(f"Error ensuring OU structure: {e}")

def ensure_nobody_user(conn):
    """Ensure the placeholder 'nobody' user exists."""
    try:
        conn.search(USER_OU_DN, f"(uid={NOBODY_UID})", attributes=["uid"])
        if not conn.entries:
            if not conn.add(NOBODY_DN, ['inetOrgPerson', 'organizationalPerson', 'person', 'top', 'chimeraPerson'], {
                'cn': 'Nobody',
                'sn': 'Placeholder',
                'givenName': 'Nobody',
                'uid': NOBODY_UID,
                'mail': 'nobody@example.com'
            }):
                raise Exception(f"Failed to create nobody user: {NOBODY_DN}")
            print(f"✅ Created nobody user: {NOBODY_DN}")
    except Exception as e:
        raise Exception(f"Error ensuring nobody user: {e}")

def ensure_user_in_ldap(conn, member):
    """Ensure a Discord member exists in LDAP, raise if fails."""
    db_username = get_username_from_db(member.id) or member.name
    db_email = get_email_from_db(member.id) or f"{db_username}@example.com"
    user_dn = f"uid={db_username},{USER_OU_DN}"

    try:
        conn.search(USER_OU_DN, f"(uid={db_username})", attributes=["uid"])
        if not conn.entries:
            # Only add objectClasses that exist in schema
            if not conn.add(user_dn, [
                'inetOrgPerson', 'organizationalPerson', 'chimeraPerson', 'person', 'top'
            ], {
                'cn': db_username,
                'sn': db_username,
                'givenName': db_username,
                'uid': db_username,
                'mail': db_email
            }):
                raise Exception(f"Failed to create LDAP user: {user_dn}")
            print(f"✅ LDAP user created: {db_username}")
        else:
            print(f"[DEBUG] User already exists in LDAP: {user_dn}")
    except Exception as e:
        raise Exception(f"Error creating user {db_username}: {e}")

def ensure_groupofnames(conn, group_name):
    group_dn = f"cn={group_name},{GROUP_OU_DN}"
    conn.search(GROUP_OU_DN, f"(cn={group_name})", attributes=["member"])
    if not conn.entries:
        conn.add(group_dn, ['groupOfNames', 'top'], {'cn': group_name, 'member': [NOBODY_DN]})
    else:
        members = set(str(m) for m in getattr(conn.entries[0], "member", []))
        if NOBODY_DN not in members:
            conn.modify(group_dn, {'member': [(MODIFY_ADD, [NOBODY_DN])]})
    return group_dn

def add_user_to_group(conn, user_dn, role_name):
    ensure_groupofnames(conn, role_name)
    conn.modify(f"cn={role_name},{GROUP_OU_DN}", {'member': [(MODIFY_ADD, [user_dn])]})

def remove_user_from_group(conn, user_dn, role_name):
    group_dn = f"cn={role_name},{GROUP_OU_DN}"
    conn.modify(group_dn, {'member': [(MODIFY_DELETE, [user_dn])]})

def ensure_posix_attributes(conn, username, member_roles, uid_number=None):
    """
    Voeg POSIX toe als de gebruiker in de juiste Linux Discord rol zit.
    """
    user_dn = f"uid={username},{USER_OU_DN}"
    allowed_roles = {"linux-root", "linux-member"}

    if allowed_roles & set(member_roles):
        gid = POSTFIX_ROOT_GID if "linux-root" in member_roles else POSTFIX_MEMBER_GID
        if uid_number is None:
            # fallback uidNumber = 10000 + hash(username) % 1000
            uid_number = 10000 + (abs(hash(username)) % 1000)

        # Voeg POSIX objectClasses toe als ze er nog niet zijn
        conn.modify(user_dn, {
            'objectClass': [(MODIFY_ADD, ['posixAccount', 'shadowAccount'])],
            'uidNumber': [(MODIFY_REPLACE, [str(uid_number)])],
            'gidNumber': [(MODIFY_REPLACE, [str(gid)])],
            'homeDirectory': [(MODIFY_REPLACE, [f"/home/{username}"])],
            'loginShell': [(MODIFY_REPLACE, ['/bin/bash'])]
        })
        print(f"➕ [ADD] POSIX enabled for {username} with gid {gid} and uid {uid_number}")
    else:
        reset_posix_attributes(conn, username)

def reset_posix_attributes(conn, username):
    """
    Verwijder POSIX objectClasses en alle gerelateerde attributen voor niet-Linux gebruikers.
    """
    user_dn = f"uid={username},{USER_OU_DN}"
    try:
        # Verwijder alle POSIX attributen
        conn.modify(user_dn, {
            'uidNumber': [(MODIFY_DELETE, [])],
            'gidNumber': [(MODIFY_DELETE, [])],
            'homeDirectory': [(MODIFY_DELETE, [])],
            'loginShell': [(MODIFY_DELETE, [])],
            'objectClass': [(MODIFY_DELETE, ['posixAccount', 'shadowAccount'])]
        })
        print(f"❌ [DEL] POSIX attributes removed for {username}")
    except Exception as e:
        print(f"❌ Failed to reset POSIX for {username}: {e}")





# ---------------- PHP Hook ----------------
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

# ---------------- Quota ----------------
def set_quota(discord_id):
    db_conn = get_database_connection()
    if not db_conn:
        return 0
    try:
        with db_conn.cursor() as cursor:
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

    db_username = get_username_from_db(discord_id)
    if not db_username:
        return quota_mb

    conn = get_ldap_connection()
    if conn:
        user_dn = f"uid={db_username},{USER_OU_DN}"
        try:
            conn.modify(user_dn, {'quota': [(MODIFY_REPLACE, [f"{quota_mb}MB"])]})
            print(f"✅ LDAP quota updated for {db_username} to {quota_mb}MB")
        finally:
            conn.unbind()
    return quota_mb

# ---------------- Core Update Function ----------------
def update_user_groups(member):
    conn = get_ldap_connection()
    if not conn:
        return

    try:
        # Ensure base users exist
        ensure_nobody_user(conn)

        # Map Discord ID to LDAP username
        db_username = get_username_from_db(member.id) or member.name
        user_dn = f"uid={db_username},{USER_OU_DN}"

        # Ensure user exists in LDAP (do NOT set POSIX yet)
        ensure_user_in_ldap(conn, member)

        # Gather role sets
        stored_roles = set(get_stored_roles(member.id))
        current_roles = set(role.name for role in member.roles if not role.is_default())
        current_role_ids = set(role.id for role in member.roles)

        # ---------------- REMOVE old roles ----------------
        for role_name in stored_roles - current_roles:
            print(f"[DEBUG] ➖ REMOVE {member.name} from group '{role_name}'")
            remove_user_from_group(conn, user_dn, role_name)
            trigger_php_hook(member.id, role_name, "")

        # ---------------- ADD new roles ----------------
        for role_name in current_roles - stored_roles:
            print(f"[DEBUG] ➕ ADD {member.name} to group '{role_name}'")
            add_user_to_group(conn, user_dn, role_name)
            trigger_php_hook(member.id, "", role_name)

        # ---------------- POSIX Attributes ----------------
        if POSTFIX_ROOT_ROLE_ID in current_role_ids:
            # Root Linux user
            ensure_posix_attributes(conn, db_username, ["linux-root"])
        elif POSTFIX_MEMBER_ROLE_ID in current_role_ids:
            # Member Linux user
            ensure_posix_attributes(conn, db_username, ["linux-member"])
        else:
            # No Linux role → disable POSIX
            reset_posix_attributes(conn, db_username)

        # ---------------- Update DB ----------------
        store_user_roles(member)

    finally:
        conn.unbind()


# ---------------- Discord Events ----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

    conn = get_ldap_connection()
    if not conn:
        print("❌ Could not connect to LDAP, aborting sync.")
        return

    try:
        ensure_ou_structure(conn)
        ensure_nobody_user(conn)
        for guild in bot.guilds:
            print(f"[DEBUG] Syncing guild: {guild.name} ({guild.id})")
            for member in guild.members:
                # skip bots
                if member.bot:
                    continue

                discord_id = str(member.id)
                username = member.name
                member_roles = [role.name for role in member.roles if role.name != "@everyone"]

                print(f"[DEBUG] Processing {username} ({discord_id}) with roles {member_roles}")

                # this will handle:
                # - ensuring LDAP entry
                # - enabling/disabling POSIX attrs
                # - updating quota
                # - firing PHP hooks
                update_user_groups(member)
                set_quota(member.id)
    except Exception as e:
        print(f"❌ Error during on_ready sync: {e}")

    finally:
        conn.unbind()
        print("✅ LDAP connection closed.")


@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        update_user_groups(after)
        set_quota(after.id)

@bot.event
async def on_member_join(member):
    update_user_groups(member)
    set_quota(member.id)

# ---------------- Run Bot ----------------
bot.run(TOKEN)

