#!/bin/bash
# Real mail servers for the mail channel's check: Dovecot (IMAP over TLS, and STARTTLS on 143)
# and Postfix (SMTP on 25 for delivery, 587 STARTTLS and 465 TLS for sending with a login).
# Two mailboxes: bbagent (the agent's) and owner (the person who writes to it).
# It rewrites this machine's mail setup and adds two users: for a throwaway Ubuntu 24.04 machine only
# (a CI runner, a VM), which is why it wants to be told so:
#     export BB_THROWAWAY_MACHINE=1; bash tests/real/mail_setup.sh && python3 tests/real/mail_check.py .
set -euo pipefail
if [ "${BB_THROWAWAY_MACHINE:-}" != "1" ]; then
  echo "this reconfigures Postfix and Dovecot and adds users: run it only on a throwaway machine, with BB_THROWAWAY_MACHINE=1" >&2
  exit 2
fi
export DEBIAN_FRONTEND=noninteractive
echo "postfix postfix/main_mailer_type select Local only" | sudo debconf-set-selections
echo "postfix postfix/mailname string mail.bbtest.lan" | sudo debconf-set-selections
sudo apt-get install -y -qq postfix dovecot-imapd openssl >/dev/null 2>&1
grep -q mail.bbtest.lan /etc/hosts || echo "127.0.0.1 mail.bbtest.lan" | sudo tee -a /etc/hosts >/dev/null
for u in bbagent owner; do id $u >/dev/null 2>&1 || sudo useradd -m -s /bin/bash $u; done
echo 'bbagent:Agent-pass-1' | sudo chpasswd
echo 'owner:Owner-pass-1' | sudo chpasswd
sudo mkdir -p /etc/bbtest && cd /etc/bbtest
if [ ! -f ca.pem ]; then
  sudo openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem -subj "/CN=bbtest CA" -days 30 2>/dev/null
  sudo openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj "/CN=mail.bbtest.lan" 2>/dev/null
  printf "subjectAltName=DNS:mail.bbtest.lan" | sudo tee san.ext >/dev/null
  sudo openssl x509 -req -in srv.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out srv.pem -days 30 -extfile san.ext 2>/dev/null
  sudo chmod 644 ca.pem srv.pem && sudo chmod 640 srv.key && sudo chgrp dovecot srv.key
fi
sudo tee /etc/dovecot/local.conf >/dev/null <<'CONF'
protocols = imap
mail_location = maildir:~/Maildir
ssl = required
ssl_cert = </etc/bbtest/srv.pem
ssl_key = </etc/bbtest/srv.key
auth_mechanisms = plain login
disable_plaintext_auth = yes
service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
  }
}
CONF
sudo postconf -e "myhostname = mail.bbtest.lan" "mydestination = mail.bbtest.lan, localhost" "home_mailbox = Maildir/" \
  "inet_interfaces = loopback-only" "smtpd_tls_cert_file = /etc/bbtest/srv.pem" "smtpd_tls_key_file = /etc/bbtest/srv.key" \
  "smtpd_tls_security_level = may" "smtpd_sasl_type = dovecot" "smtpd_sasl_path = private/auth" "smtpd_sasl_auth_enable = yes" \
  "smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated defer_unauth_destination" "mailbox_size_limit = 0" \
  "message_size_limit = 52428800"
sudo postconf -M "submission/inet=submission inet n - y - - smtpd"
sudo postconf -P "submission/inet/smtpd_tls_security_level=encrypt" "submission/inet/smtpd_sasl_auth_enable=yes"
sudo postconf -M "smtps/inet=smtps inet n - y - - smtpd"
sudo postconf -P "smtps/inet/smtpd_tls_wrappermode=yes" "smtps/inet/smtpd_sasl_auth_enable=yes"
sudo cp /etc/bbtest/srv.key /etc/bbtest/postfix.key && sudo chown root:root /etc/bbtest/postfix.key && sudo chmod 600 /etc/bbtest/postfix.key
sudo postconf -e "smtpd_tls_key_file = /etc/bbtest/postfix.key"
sudo systemctl restart dovecot postfix
sleep 2
sudo ss -ltnp | grep -E ":(25|143|465|587|993)\s" | awk '{print $4, $6}' | sort
dovecot --version; postconf -d mail_version | head -1
