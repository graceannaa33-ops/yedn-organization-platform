"""Blueprint registration."""


def register_blueprints(app):
    from routes.admin import bp as admin_bp
    from routes.auth import bp as auth_bp
    from routes.community import bp as community_bp
    from routes.content import bp as content_bp
    from routes.distributions import bp as distributions_bp
    from routes.finance import bp as finance_bp
    from routes.members import bp as members_bp
    from routes.partner import bp as partner_bp
    from routes.payments import bp as payments_bp
    from routes.projects import bp as projects_bp
    from routes.public import bp as public_bp
    from routes.reports import bp as reports_bp
    from routes.transparency import bp as transparency_bp

    for bp in (public_bp, transparency_bp, auth_bp, members_bp, payments_bp, admin_bp, community_bp,
               finance_bp, projects_bp, distributions_bp, reports_bp, partner_bp, content_bp):
        app.register_blueprint(bp)
