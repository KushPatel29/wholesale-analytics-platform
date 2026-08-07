# User Invite + Password Set Runbook

## Summary
- Admin user creation now sends a password setup invite email automatically.
- Password reset and resend invite are email-based and use single-use, expiring hashed tokens.
- SMTP relay mode is supported without SMTP authentication.

## Required Environment
Set these values in `/etc/wholesale_analytics/wholesale.env` (or your deployment env source):

```env
SMTP_SERVER=wholesale-provisions-com.mail.protection.outlook.com
SMTP_PORT=25
SMTP_USE_TLS=0
MAIL_FROM=Wholesale Analytics <no-reply@wholesaleprovisions.example.com>
INVITES_ENABLED=1
RESET_TOKEN_TTL_SECONDS=86400
APP_PUBLIC_BASE_URL=https://<public-host>
```

Notes:
- Do not set `SMTP_USER` / `SMTP_PASS` for relay mode.
- If `SMTP_USE_TLS=1`, the app attempts STARTTLS and falls back to plain SMTP on failure.

## Deploy
1. Deploy application code.
2. Restart the service so new environment variables are loaded.
3. Verify auth DB schema upgrade created `password_reset_tokens`.
4. In Admin Portal, create a test user and confirm invite email delivery.
5. Open invite link, set password, and confirm login/redirect.
6. Attempt link reuse and confirm it is rejected.
7. Test **Resend Invite** and confirm previous invite token is invalidated.

## Rollback
If email flow issues occur:

1. Disable invite/reset sending immediately:
   - `INVITES_ENABLED=0`
2. Restart application.
3. Existing auth/login behavior remains operational.
4. Re-enable after SMTP or DNS issues are resolved.

The `password_reset_tokens` table is additive and safe to keep during rollback.

