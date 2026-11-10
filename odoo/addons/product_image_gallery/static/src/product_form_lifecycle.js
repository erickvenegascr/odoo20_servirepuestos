import { useScope } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";

const PRODUCT_MODELS = new Set(["product.template", "product.product"]);

// Odoo 20 awaits x2many subviews before the first model load. Navigation can
// destroy the form during that wait. Cancel its abandoned load before it calls
// the component-scoped ORM; preserve real failures from every live form.
export function protectProductLoad(model, scope) {
    const load = model.load.bind(model);
    model.load = (...args) => scope.isDestroyed() ? new Promise(() => {}) : load(...args);
}

patch(FormController.prototype, {
    setup() {
        const scope = useScope();
        super.setup(...arguments);
        if (PRODUCT_MODELS.has(this.props.resModel)) {
            protectProductLoad(this.model, scope);
        }
    },
});
