"""Fixtures for testing shared modules together; never imported at runtime."""


class SharedSuiteSetup:
    """Supply the operator settings required by the combined installation.

    Explicit restricted users in authorization tests keep their own settings.
    Sales fixtures receive a real alternate salesperson, without bypassing the
    production constraint. Accounting fixture builders can assign payment terms.
    """

    @classmethod
    def default_env_context(cls):
        context = super().default_env_context()
        if "sale.pos.salesperson" in cls.env.registry:
            salesperson = cls.env["sale.pos.salesperson"].sudo().create({
                "name": "Shared native fixture salesperson", "company_id": False,
            })
            context = {**context, "default_alternate_salesperson_id": salesperson.id}
        return context

    @classmethod
    def setup_independent_user(cls):
        user = super().setup_independent_user()
        if user and "restrict_sale_payment_term_changes" in user._fields:
            user.sudo().restrict_sale_payment_term_changes = False
        return user

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        users = cls.env.user | getattr(cls, "_test_user", cls.env.user)
        if "restrict_sale_payment_term_changes" in users._fields:
            users.sudo().restrict_sale_payment_term_changes = False
        if "pos.config" in cls.env.registry and "opening_cash_count_required" in cls.env["pos.config"]._fields:
            cls.env["pos.config"].sudo().search([
                ("company_id", "=", cls.env.company.id),
            ]).opening_cash_count_required = False
            cls.env = cls.env(context=dict(
                cls.env.context, default_opening_cash_count_required=False,
            ))
        if "sale.pos.salesperson" in cls.env.registry:
            salesperson = cls.env["sale.pos.salesperson"].sudo().create({
                "name": "Shared suite salesperson", "company_id": False,
            })
            cls.env = cls.env(context=dict(
                cls.env.context, default_alternate_salesperson_id=salesperson.id,
            ))
