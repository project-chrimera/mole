# Mole 🐀

A Discord → MySQL + LDAP sync bot.

This bot automatically keeps Discord roles and members synchronized with a MySQL database and an LDAP directory.  
It can also trigger external system hooks (e.g. PHP scripts) when roles change.

---

## ✨ Features
- Syncs **Discord roles → LDAP groups**
- Syncs **Discord users → LDAP users**
- Stores users and roles in **MySQL**
- Handles **role renames** gracefully
- Triggers an external **hook script** when a role changes
- Maintains a placeholder `nobody` user in LDAP (required so LDAP groups are never empty)
- Optionally manages **POSIX attributes** (uid/gid/home/shell) for Linux integration
- Supports per-role **disk quota values**

---

## ⚙️ Requirements
- Python 3.8+
- A running **MySQL/MariaDB** chimerea php-main setup. 
- A running **LDAP server** (OpenLDAP or compatible)
- A **Discord bot token**
- (Optional) A **PHP script** for role hooks

---



## LDAP setup.
create LDAP. with a ou=users and ou=groups in the base structure.
```
dn: ou=users,dc=yetanotherprojecttosavetheworld,dc=org
objectClass: organizationalUnit
ou: users

dn: ou=groups,dc=yetanotherprojecttosavetheworld,dc=org
objectClass: organizationalUnit
ou: groups
```

Add the chimera structure
ldapadd -Y EXTERNAL -H ldapi:/// -f chimera.ldif


