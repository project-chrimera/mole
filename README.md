# Mole 🐀

A Discord → MySQL + LDAP sync bot.

This bot automatically keeps Discord roles and members synchronized with a MySQL database and an LDAP directory.  
It can also trigger external system hooks (e.g. PHP scripts) when roles change.

---

## ✨ Features
- Syncs **Discord roles → LDAP groups**  
- Syncs **Discord users → LDAP users**  
- Stores users and roles in **MySQL**  
- Detects and handles **role renames**  
- Runs a **hook script** when a role changes  
- Maintains a placeholder `nobody` user in LDAP (LDAP does not like empty groups)

---

