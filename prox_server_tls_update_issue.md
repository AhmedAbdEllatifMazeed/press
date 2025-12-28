# ProxySQL admin password refresh for TLS play

## On The Agent Server (Get the real password)
  ```sql
  sqlite3 /var/lib/proxysql/proxysql.db \
    "SELECT variable_value FROM global_variables WHERE variable_name='admin-admin_credentials';"
  ```

## On Press server

- Updated the Proxy Server doc by setting a new `proxysql_admin_password` (UI password field).
   ```python
    import frappe
    from frappe.utils.password import set_encrypted_password
    set_encrypted_password("Proxy Server", "<proxy_name>", "<new_password>", "proxysql_admin_password")
  ```
- Verified the stored secret in bench console via `doc.get_password("proxysql_admin_password")` for the target Proxy Server.
  ```python
  import frappe
  doc = frappe.get_doc("Proxy Server", "<proxy_name>")
  doc.get_password("proxysql_admin_password")
  ```
- 
- Reran the TLS play so Ansible picks up the new password:
  ```python
  import frappe
  from press.press.doctype.tls_certificate.tls_certificate import update_server_tls_certifcate

  server = frappe.get_doc("Proxy Server", "<proxy_name>")
  cert = server.get_certificate()
  update_server_tls_certifcate(server, cert)
  ```
- Confirmed the latest Ansible Play entry now shows the refreshed `proxysql_admin_password` in its variables snapshot.

## Other commands will help

### Inside the proxy container (On The proxy agent server)
  ```bash
  apt-get update
  apt-get install -y default-mysql-client
  apt-get update && apt-get install -y sqlite3
  ```


### On the proxy agent (Set the proxysql password)
  ```bash
  cd /home/frappe/agent
  source env/bin/activate
  agent setup proxysql --password "NEWPASS"
  ```