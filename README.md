# Help Access Mail Relay

A small service on your home server that lets Claude **send email as a shared mailbox, with attachments**.
The Microsoft 365 connector can do neither.

```
Claude  ──stage_attachment──▶  Relay (your server)  ──holds the file briefly
Claude  ──send_mail─────────▶  Relay  ──Microsoft Graph──▶  email goes out as jcarter@helpaccess.ca
                                       └─ deletes the file after sending (or after 24 h)
```

Exchange still adds each mailbox's signature, because the mail leaves through the mailbox itself.

**Tools Claude gets:** `stage_attachment`, `send_mail`, `list_staged_attachments`,
`delete_staged_attachment`, `relay_status`.

---

## Part 1: Microsoft setup (about 20 minutes)

### 1a. Register the app
1. Go to **entra.microsoft.com** > **App registrations** > **New registration**.
2. Name: `Help Access Mail Relay`. Single tenant. No redirect URI. Click **Register**.
3. Copy the **Application (client) ID** and **Directory (tenant) ID**. These are `CLIENT_ID` and `TENANT_ID`.
4. **Certificates & secrets** > **New client secret** > 24 months. Copy the **Value** right away, not the Secret ID.
   This is `CLIENT_SECRET`. Put a reminder in your calendar a month before it expires.

> **Do not add any API permissions or click "Grant admin consent".** A mail permission granted here applies to
> every mailbox in the company and would override the restriction in step 1b.

### 1b. Limit the app to only the mailboxes it needs
Microsoft's current method is called RBAC for Applications. In **Entra** > **Enterprise applications**, search for
`Help Access Mail Relay` and copy its **Object ID** (the enterprise application, not the app registration).

In PowerShell (`Install-Module ExchangeOnlineManagement` first if you don't have it):

```powershell
Connect-ExchangeOnline

New-ServicePrincipal -AppId <CLIENT_ID> -ObjectId <ENTERPRISE_APP_OBJECT_ID> -DisplayName "Help Access Mail Relay"

New-ManagementScope -Name "RelayMailboxes" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'jcarter@helpaccess.ca' -or PrimarySmtpAddress -eq 'vhale@helpaccess.ca'"

New-ManagementRoleAssignment -App <CLIENT_ID> -Role "Application Mail.Send" -CustomResourceScope "RelayMailboxes"
```

Check it. The first should show `InScope = True`, the second `False`:

```powershell
Test-ServicePrincipalAuthorization -Identity "Help Access Mail Relay" -Resource jcarter@helpaccess.ca | Format-Table
Test-ServicePrincipalAuthorization -Identity "Help Access Mail Relay" -Resource mteskie@helpaccess.ca | Format-Table
```

Changes can take 30 minutes to 2 hours to apply. To add a mailbox later, edit the filter with
`Set-ManagementScope -Identity RelayMailboxes -RecipientRestrictionFilter "..."` and add it to `ALLOWED_MAILBOXES`.

Source: Microsoft, "Role Based Access Control for Applications in Exchange Online"
(learn.microsoft.com/graph/auth-limit-mailbox-access).

---

## Part 2: Deploy with GitHub and Coolify

1. Create a **new private** GitHub repository named `help-access-mail-relay`. Do not use the website repository.
   Upload everything in this folder, keeping the layout (`app/`, `tests/`, `Dockerfile`, `requirements.txt`, ...).
2. In **Coolify**: New Resource > the GitHub repository > build pack **Dockerfile**, port **8000**.
3. **Environment variables**: copy them from `.env.example`. Fill in `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`.
   Make the secret path with: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`, and put the result in
   `MCP_PATH_SECRET`. Leave `DRY_RUN=1` for now.
4. **Public address**: add a hostname such as `mailer.helpaccess.ca` the same way your other Coolify apps are
   exposed through the Cloudflare Tunnel, and set the same domain in the app's Domains field in Coolify.
5. Deploy. Then open `https://mailer.helpaccess.ca/healthz`. It should say `ok`.

---

## Part 3: Connect it to Claude

In Claude: **Settings** > **Connectors** > **Add custom connector**. The exact menu names may differ, and custom
connectors depend on your plan.

- URL: `https://mailer.helpaccess.ca/<MCP_PATH_SECRET>/mcp`
- Leave any OAuth fields empty.

Connectors belong to one Claude account. Add the same URL in each account that should be able to send
(for example James's account).

---

## Part 4: Test, in this order

1. **Dry run** (`DRY_RUN=1`). Ask Claude: *"Use relay_status."* You should see `dry_run: true`.
   Then: *"Make a one-page PDF, stage it, and send it from jcarter@helpaccess.ca to mteskie@helpaccess.ca with
   subject 'Relay test'."* The result says `dry_run` and nothing is sent.
2. **Go live**: in Coolify set `DRY_RUN=0` and redeploy. Ask Claude for `relay_status` again.
   `microsoft_login` must say `ok`.
3. **Real send**: repeat the test email. Check that the PDF is attached, the signature is at the bottom, and a copy is
   in James's Sent Items.

---

## Part 5: Instructions for an agent that sends files

Paste this into the agent's instructions:

> To email a file: (1) create the file; (2) run `base64 -w0 <file>` and `stat -c %s <file>`;
> (3) call `stage_attachment` with the file name, the base64 text unchanged, and the size as `expected_size_bytes`;
> (4) call `send_mail` with `attachment_ids`. Never add a signature or sign-off: Exchange adds it.
> If a call fails, read the error message, fix the cause and try once more. Only stage again if the error says the
> attachment was not found.

---

## Limits you should know about

- **Claude has to type the file's contents into the call.** That is how a cloud run passes a file to your server.
  It is fine for small reports (roughly 8 KB is a few thousand tokens). As a rough guide, 100 KB of file is tens of
  thousands of tokens, so keep generated PDFs small. Hard limit: 2 MB per file, 2.5 MB per email.
- `expected_size_bytes` catches truncated uploads, and PDFs, images and Office files must have a valid file header.
- **Send only.** It does not read mail or move it between folders.
- **No threading and no drafts**: replies go out as new emails and send immediately.
- Staged files live inside the container. A redeploy clears them, which is harmless.

## Security notes

- **The connector URL is the password.** Anyone who has it can send as the allowed mailboxes. Don't paste it in chats
  or emails. To rotate it: change `MCP_PATH_SECRET`, redeploy, and update the connector.
- Three guards limit the damage: `ALLOWED_MAILBOXES`, `INTERNAL_ONLY_MAILBOXES` (for example Victoria's mailbox
  can only email `@helpaccess.ca`) and 30 sends per hour.
- Coolify's logs list every send (mailbox, recipients, subject, file names). They never contain the secret URL.

## Troubleshooting

| You see | Likely cause |
|---|---|
| `ErrorAccessDenied` | Exchange role or scope isn't set, doesn't include that mailbox, or is still propagating (up to 2 hours). Run `Test-ServicePrincipalAuthorization`. |
| `AADSTS7000215` | `CLIENT_SECRET` holds the Secret ID instead of the Value. |
| `AADSTS700016` / `AADSTS90002` | Wrong `CLIENT_ID` or `TENANT_ID`. |
| Connector says 404 | The URL path doesn't exactly match `MCP_PATH_SECRET`. |
| "attachment was not found" | It was already sent, expired, or the service restarted. Stage it again. |

## Run the tests yourself

`pip install -r requirements.txt` then `python3 tests/smoke_test.py`. They start the service against a fake
Microsoft and check 30 behaviours.
