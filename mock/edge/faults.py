"""Where each fault of the catalogue (models.CATALOGUE) takes effect - the one place to look when adding a fault.

edge   - app.Edge answers the AWS call itself (before or instead of forwarding it)
world  - seed.World changes the seeded data (a lock item, a hidden document)
erp    - erp.app.ErpMock misbehaves
sql    - edge.sqlcontrol / edge.sqlproxy change the database or its front
"""

HANDLED_BY = {
    "idp.kms_denied": "edge", "idp.lookup_denied": "edge", "idp.not_found": "world",
    "erp.unreachable": "erp", "erp.slow": "erp", "erp.row_rejected": "erp", "erp.session_expired": "erp",
    "sql.unreachable": "sql", "sql.login_refused": "sql", "sql.routine_error": "sql", "sql.slow": "sql",
    "secrets.unavailable": "edge", "lock.held": "world", "lambda.invoke_denied": "edge",
}
