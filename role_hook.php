#!/usr/bin/php
<?php

chdir("/to/your/chrimeria-main-php");
include("./include.php");

/**
 * Usage: php role_hook.php <discord_id> <old_role> <new_role>
 */

if ($argc < 4) {
    fwrite(STDERR, "Usage: php {$argv[0]} <discord_id> <old_role> <new_role>\n");
    exit(1);
}

$discord_id = $argv[1];
$old_role   = $argv[2]; // role removed
$new_role   = $argv[3]; // role added

echo "[DEBUG] Discord ID: {$discord_id}\n";

//MAIN
$aRoles = get_roles($discord_id);
$roleNames = array_column($aRoles, 'name'); // ['member', 'newMember']

// ---------------- REMOVE old role ----------------
if (!empty($old_role)) {
    echo "[DEBUG] REMOVE role: {$old_role}\n";

    $email = get_user_email_by_discord_id($discord_id);
    if (!$email) {
        echo "[ERROR] Could not find email for Discord ID {$discord_id}\n";
    } else {
        switch ($old_role) {
            case 'member':
            case 'newMember':
                if (!in_array('member', $roleNames, true) && !in_array('newMember', $roleNames, true)) {
                  call_wp_role_api($email, 'subscriber', 'remove');
                  call_bbpress_role_api($email, 'bbp_blocked');
                }
                break;

            case 'wpAdmin':
                call_wp_role_api($email, 'administrator', 'remove');
                // Downgrade bbPress only if currently keymaster
                $current_bb_role = get_bbpress_role($email);
                if ($current_bb_role === 'bbp_keymaster') {
                    call_bbpress_role_api($email, 'bbp_participant');
                }
                break;

            default:
                // Other removed roles
                break;
        }
    }
}

// ---------------- ADD new role ----------------
if (!empty($new_role)) {
    echo "[DEBUG] ADD role: {$new_role}\n";

    $email = get_user_email_by_discord_id($discord_id);
    if (!$email) {
        echo "[ERROR] Could not find email for Discord ID {$discord_id}\n";
    } else {
        switch ($new_role) {
            case 'member':
            case 'newMember':
                call_wp_role_api($email, 'subscriber', 'add');
                call_bbpress_role_api($email, 'bbp_participant');
                break;

            case 'wpAdmin':
                call_wp_role_api($email, 'administrator', 'add');
                // Optionally, elevate bbPress to keymaster
                call_bbpress_role_api($email, 'bbp_keymaster');
                break;

            default:
                // Other added roles
                break;
        }
    }
}

echo "✅ PHP hook executed.\n";

