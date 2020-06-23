ProductManager = function () {
    this.field_type = {}
    this.controllers = {
                        'secondary' : function(t) { return function(x,y,z) { t.RecipeSetChanged(x,y,z) } },
                        'primary' : function (t) { return function(x,y,z) { t.ChangeAll(x,y,z) } }
                       }
    this.my_regex = /^.+?(\d{1,})$/

}

ProductManager.prototype = new PrimarySecondary()

ProductManager.prototype.RecipeSetChanged = function(new_product_id,recipeset_id,callback) { 
    var params = {"tg_format" : "json",
                  "tg_random" : new Date().getTime(),
                  "product_id" : new_product_id,
                  "recipeset_id" : recipeset_id }
    AjaxLoader.prototype.add_loader('product_recipeset_' + recipeset_id) 
    var d = loadJSONDoc('/jobs/change_product_recipeset' + "?" + queryString(params))
    // I wish we could just pass the callback var to priorityChanged
    // Reason we can't is because it each call uses the same pointer value it seems! 
    d.addCallback(ProductManager.prototype.valueChanged,callback['function'],callback['args']['element_id'],callback['args']['value']) //mochikit's built in currying...
}
