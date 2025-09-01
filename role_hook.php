#!/usr/bin/php
<?php

chdir("/home/yap2stw/www/");
include("./include.php");

$dokuwikiPath = "/home/yap2stw/dokuwiki/conf/users.auth.php";
/**
 * Usage: php role_hook.php <discord_id> <old_role> <new_role>
 */

if ($argc < 4) {
    fwrite(STDERR, "Usage: php {$argv[0]} <discord_id> <old_role> <new_role>\n");
    exit(1);
}

$discord_id = $argv[1];
$old_role   = $argv[2];
$new_role   = $argv[3];

echo "[DEBUG] Discord ID: {$discord_id}\n";

/**
 * Helper: Generate random password
 */
function gen_random_password($length = 12) {
    return bin2hex(random_bytes($length/2));
}

/**
 * Helper: Create a DokuWiki user if it doesn't exist
 */
/**
 * Create a new DokuWiki user
 *
 * @param string $username The username
 * @param string $email    The user email
 * @param string $fullName The display name
 * @param array  $groups   Optional array of groups to assign
 * @param string $userfile Path to users.auth.php
 * @return string|false    Returns generated password on success, false on failure
 */
function add_dokuwiki_user($username, $email, $fullName = '', $groups = [], $userfile) {
    if (!file_exists($userfile)) {
        echo "[ERROR] users.auth.php not found at $userfile\n";
        return false;
    }

    $lines = file($userfile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);

    // Check if user exists
    foreach ($lines as $line) {
        if (strpos($line, $username . ':') === 0) {
            echo "[INFO] User $username already exists\n";
            return false;
        }
    }

    // Generate random password
    $password = bin2hex(random_bytes(6)); // 12 chars
    $hash = password_hash($password, PASSWORD_BCRYPT);

    // Prepare groups string
    $groupsStr = implode(',', $groups);

    // Construct line: username:hash:fullName:email:groups
    $line = "{$username}:{$hash}:{$fullName}:{$email}:{$groupsStr}";

    // Append to users.auth.php
    file_put_contents($userfile, $line . "\n", FILE_APPEND);

    echo "[INFO] Created user $username with password: $password\n";
    return $password;
}



/**
 * Remove a group from a DokuWiki user safely, keeping email intact
 *
 * @param string $username The DokuWiki username
 * @param string $email    The email of the user (kept intact)
 * @param string $group    The group to remove
 * @param string $userfile Path to users.auth.php
 */
function del_dokuwiki_group($username, $email, $group, $userfile = "/home/yap2stw/dokuwiki/conf/users.auth.php") {
    if (!file_exists($userfile)) {
        echo "[ERROR] users.auth.php not found at $userfile\n";
        return false;
    }

    $lines = file($userfile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    foreach ($lines as &$line) {
        if (strpos($line, $username . ':') === 0) {
            // Split only into 5 parts
            $parts = explode(':', $line, 5);

            // Ensure all fields exist
            while (count($parts) < 5) $parts[] = '';

            // Preserve email
            $parts[3] = $email;

            $groups = array_filter(explode(',', $parts[4])); // current groups

            if (in_array($group, $groups)) {
                $groups = array_diff($groups, [$group]);
                $parts[4] = implode(',', $groups);
                $line = implode(':', $parts);
                echo "[INFO] Removed group '$group' from user $username\n";
            } else {
                echo "[INFO] User $username does not have group '$group'\n";
            }

            // Save back
            file_put_contents($userfile, implode("\n", $lines)."\n");
            return true;
        }
    }

    echo "[ERROR] User $username not found\n";
    return false;
}


/**
 * Add a group to a DokuWiki user safely, keeping email intact
 *
 * @param string $username The DokuWiki username
 * @param string $email    The email of the user (kept intact)
 * @param string $group    The group to add
 * @param string $userfile Path to users.auth.php
 */
function add_dokuwiki_group($username, $email, $group, $userfile = "/home/yap2stw/dokuwiki/conf/users.auth.php") {
    if (!file_exists($userfile)) {
        echo "[ERROR] users.auth.php not found at $userfile\n";
        return false;
    }

    $lines = file($userfile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    foreach ($lines as &$line) {
        if (strpos($line, $username . ':') === 0) {
            // Split only into 5 parts
            $parts = explode(':', $line, 5);

            // Ensure all fields exist
            while (count($parts) < 5) $parts[] = '';

            // Preserve the email passed as argument
            $parts[3] = $email;

            $groups = array_filter(explode(',', $parts[4])); // current groups

            if (!in_array($group, $groups)) {
                $groups[] = $group;
                $parts[4] = implode(',', $groups);
                $line = implode(':', $parts);
                echo "[INFO] Added group '$group' to user $username\n";
            } else {
                echo "[INFO] User $username already has group '$group'\n";
            }

            // Save back
            file_put_contents($userfile, implode("\n", $lines)."\n");
            return true;
        }
    }

    echo "[ERROR] User $username not found\n";
    return false;
}




$email = get_user_email_by_discord_id($discord_id);
$user = get_username($discord_id);

// ---------------- REMOVE old role ----------------
if (!empty($old_role)) {
    echo "[DEBUG] REMOVE role: {$old_role}\n";
    switch ($old_role) {
        case 'member':
        case 'newMember':
            call_wp_role_api($email, 'subscriber', 'remove');
            call_bbpress_role_api($email, 'bbp_blocked');

	    del_dokuwiki_group($user,$email,'member',$dokuwikiPath);

            break;
        case 'wpAdmin':
            call_wp_role_api($email, 'administrator', 'remove');
            $current_bb_role = get_bbpress_role($email);
            if ($current_bb_role === 'bbp_keymaster') {
                call_bbpress_role_api($email, 'bbp_participant');
            }
            break;
        case 'wikiAdmin':
	    del_dokuwiki_group($user,'admin',$dokuwikiPath);
            break;
    }
}

// ---------------- ADD new role ----------------
if (!empty($new_role)) {
    echo "[DEBUG] ADD role: {$new_role}\n";

    // Create DokuWiki user if not exists
//    create_dokuwiki_user($username, $email);

    switch ($new_role) {
        case 'member':
            add_dokuwiki_user($user,$email,$user,['discord']);
	    add_dokuwiki_group($user,$email,'member');
        case 'newMember':
            call_wp_role_api($email, 'subscriber', 'add');
            call_bbpress_role_api($email, 'bbp_participant');
            set_wiki_admin($username);
            break;
        case 'wpAdmin':
            call_wp_role_api($email, 'administrator', 'add');
            call_bbpress_role_api($email, 'bbp_keymaster');
            break;
        case 'wikiAdmin':
            add_dokuwiki_user($user,$email,$user,['discord'],$dokuwikiPath);
	    add_dokuwiki_group($user,$email,'admin',$dokuwikiPath);
	    add_dokuwiki_group($user,$email,'member',$dokuwikiPath);
            break;
    }
}

echo "✅ PHP hook executed.\n";

