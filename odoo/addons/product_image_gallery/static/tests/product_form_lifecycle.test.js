import { animationFrame, expect, test } from "@odoo/hoot";
import { Component, onWillStart, useScope, xml } from "@odoo/owl";
import { defineModels, destroyApp, fields, models, mountView, mountWithCleanup, onRpc, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { defineMailModels } from "@mail/../tests/mail_test_helpers";
import { useService } from "@web/core/utils/hooks";
import { FormController } from "@web/views/form/form_controller";
import { RelationalModel } from "@web/model/relational_model/relational_model";
import { protectProductLoad } from "@product_image_gallery/product_form_lifecycle";

class ProductTemplate extends models.Model {
    _name = "product.template";
    name = fields.Char();
    gallery_ids = fields.One2many({ relation: "product.gallery.image", relation_field: "product_id" });
    _records = [{ id: 1, name: "Synthetic product", gallery_ids: [] }];
}
class ProductGalleryImage extends models.Model {
    _name = "product.gallery.image";
    name = fields.Char();
    product_id = fields.Many2one({ relation: "product.template" });
    _views = { list: '<list><field name="name"/></list>' };
}
defineMailModels();
defineModels([ProductTemplate, ProductGalleryImage]);

test("abandoned product form cannot start web_read after delayed subviews", async () => {
    const deferred = Promise.withResolvers();
    const entered = Promise.withResolvers();
    let abandoned = false;
    patchWithCleanup(RelationalModel.prototype, {
        load(...args) {
            if (abandoned) { expect.step("late native model load"); }
            return super.load(...args);
        },
    });
    patchWithCleanup(FormController.prototype, {
        setup() {
            super.setup(...arguments);
            const info = Object.values(this.archInfo.fieldNodes).find(field => field.name === "gallery_ids");
            info.views = {};
            const loadViews = this.viewService.loadViews.bind(this.viewService);
            this.viewService.loadViews = async (...args) => {
                const result = await loadViews(...args);
                expect.step("subview requested");
                entered.resolve();
                await deferred.promise;
                return result;
            };
        },
    });
    onRpc("product.template", "web_read", () => expect.step("unexpected product read"));
    mountView({ resModel: "product.template", type: "form", resId: 1,
        arch: '<form><field name="name"/><field name="gallery_ids" mode="list"/></form>' });
    await entered.promise;
    expect.verifySteps(["subview requested"]);
    destroyApp();
    abandoned = true;
    deferred.resolve();
    await animationFrame();
    expect.verifySteps([]);
});

test("a destroyed component cannot invoke its ORM after an uncancellable wait", async () => {
    const deferred = Promise.withResolvers();
    const entered = Promise.withResolvers();
    onRpc("product.template", "web_read", () => expect.step("unexpected product read"));
    class PendingProduct extends Component {
        static template = xml`<div/>`;
        setup() {
            const orm = useService("orm");
            const model = { load: () => {
                expect.step("late native model load");
                return orm.webRead("product.template", [1], { name: {} });
            } };
            protectProductLoad(model, useScope());
            onWillStart(async () => {
                expect.step("waiting before model load");
                entered.resolve();
                await deferred.promise;
                await model.load();
            });
        }
    }
    mountWithCleanup(PendingProduct);
    await entered.promise;
    expect.verifySteps(["waiting before model load"]);
    destroyApp();
    deferred.resolve();
    await animationFrame();
    expect.verifySteps([]);
});

test("live product loads retain results and real failures", async () => {
    const model = { load: async (value) => {
        if (value === "failure") { throw new Error("real server failure"); }
        return value;
    } };
    protectProductLoad(model, { isDestroyed: () => false });
    expect(await model.load("loaded")).toBe("loaded");
    await expect(model.load("failure")).rejects.toThrow("real server failure");
});
