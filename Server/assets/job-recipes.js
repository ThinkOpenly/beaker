
// This program is free software; you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation; either version 2 of the License, or
// (at your option) any later version.

;(function () {

window.JobRecipesView = Backbone.View.extend({
    tagName: 'table',
    className: 'job-recipes table',
    initialize: function () {
        this.render();
        // If the page URL has an anchor pointing at a specific recipe set, 
        // highlight it.
        var highlight_recipeset = _.bind(function () {
            this.$('tbody')
                .removeClass('highlight')
                .filter(location.hash)
                .addClass('highlight');
        }, this);
        highlight_recipeset();
        $(window).on('hashchange', highlight_recipeset);
    },
    render: function () {
        var tbodies = _.map(this.model.get('recipesets'), function (recipeset) {
            return new RecipeSetView({model: recipeset}).render().el;
        });
        this.$el.empty().append(tbodies);
    },
});

var RecipeSetView = Backbone.View.extend({
    tagName: 'tbody',
    template: JST['recipeset'],
    events: {
        'click .priority': 'change_priority',
        'click .cancel': 'cancel',
        'click .waive': 'waive',
        'click .unwaive': 'unwaive',
    },
    initialize: function () {
        this.listenTo(this.model, 'change:status change:waived change:can_change_priority change:can_cancel change:can_waive', this.render);
        this.listenTo(this.model, 'cancelling', this.cancelling);
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes))
                .attr('id', 'set' + this.model.get('id'));
        new RecipeSetCommentsLink({model: this.model}).$el.appendTo(this.$('td.recipeset-comments'));
        var $el = this.$el;
        _.each(this.model.get('machine_recipes'), function (recipe) {
            $el.append(new JobRecipeRow({model: recipe}).render().el);
            _.each(recipe.get('guest_recipes'), function (guest) {
                $el.append(new JobRecipeRow({model: guest, guest: true}).render().el);
            });
        });
        return this;
    },
    change_priority: function () {
        new RecipeSetPriorityModal({model: this.model});
    },
    cancel: function () {
        new RecipeSetCancelModal({model: this.model});
    },
    waive: function () {
        new RecipeSetWaiveModal({model: this.model});
    },
    unwaive: function () {
        this.$('button.unwaive').button('loading');
        this.model.save({waived: false}, {patch: true, wait: true})
            .fail(_.bind(this.unwaive_error, this));
    },
    unwaive_error: function (xhr) {
        this.$('button.unwaive').button('reset');
        growl_for_xhr(xhr, 'Error unwaiving ' + this.model.get('t_id'));
    },
    cancelling: function () {
        this.$('button.cancel').button('loading');
    },
});

var RecipeSetCommentsLink = Backbone.View.extend({
    template: JST['recipeset-comments-link'],
    initialize: function () {
        this.listenTo(this.model.get('comments'), 'reset add remove', this.render);
        this.render();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
        this.$('.comments-link').beaker_popover({
            model: this.model,
            view_type: RecipeSetCommentsPopover,
        });
        return this;
    },
});

window.RecipeSetCommentsPopover = BeakerPopoverView.extend({
    className: 'popover recipeset-comments-popover',
    render: function () {
        BeakerPopoverView.prototype.render.apply(this);
        new RecipeSetCommentsList({model: this.model}).$el
            .appendTo(this.$('.popover-content'));
        if (this.model.get('can_comment')) {
            new RecipeSetCommentForm({model: this.model}).$el
                .appendTo(this.$('.popover-content'));
        }
    },
});

var RecipeSetCommentsList = Backbone.View.extend({
    template: JST['recipeset-comments'],
    initialize: function (options) {
        this.listenTo(this.model.get('comments'), 'reset add remove', this.render);
        this.render();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
        if (!this.model.get('comments').isEmpty()) {
            this.$el.addClass('comments');
        }
        return this;
    },
});

var RecipeSetCommentForm = Backbone.View.extend({
    events: {
        'submit form.new-comment': 'add_comment',
    },
    template: JST['recipeset-comment-form'],
    className: 'recipeset-comment-form',
    initialize: function (options) {
        this.render();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
        return this;
    },
    add_comment: function (evt) {
        evt.preventDefault();
        this.$('.sync-status').empty();
        this.$('button').button('loading');
        var comment = this.$('textarea[name=comment]').val();
        this.model.get('comments').create({comment: comment}, {
            wait: true,
            success: _.bind(this.save_success, this),
            error: _.bind(this.save_error, this),
        });
    },
    save_success: function (model, xhr, options) {
        this.$('button').button('reset');
        this.$('textarea[name=comment]').val('');
    },
    save_error: function (model, xhr, options) {
        alert_for_xhr(xhr).appendTo(this.$('.sync-status'));
        this.$('button').button('reset');
    },
});

var JobRecipeRow = Backbone.View.extend({
    tagName: 'tr',
    template: JST['job-recipe'],
    events: {
        'click .recipe-reviewed': 'toggle_recipe_reviewed',
    },
    initialize: function (options) {
        this.guest = !!options.guest;
        this.listenTo(this.model, 'change', this.render);
    },
    render: function () {
        this.$el.html(this.template(_.extend({guest: this.guest}, this.model.attributes)));
        var status = this.model.get('status');
        if (status == 'Running' || status == 'Completed') {
            var progressbar = new RecipeProgressBar({model: this.model});
            this.$('.recipe-status').append(progressbar.el);
        } else if (status == 'Cancelled' || status == 'Aborted') {
            $('<span/>')
                .addClass('label label-warning')
                .text(status)
                .appendTo(this.$('.recipe-status'));
        } else {
            this.$('.recipe-status').text(status);
        }
        if (this.guest)
            this.$el.addClass('guestrecipe');
        return this;
    },
    toggle_recipe_reviewed: function (evt) {
        var reviewed = evt.target.checked;
        this.model.save({reviewed: reviewed}, {patch: true, silent: true});
    },
});

var RecipeSetPriorityModal = Backbone.View.extend({
    tagName: 'div',
    className: 'modal recipeset-priority',
    template: JST['recipeset-priority'],
    events: {
        'submit form': 'submit',
        'hidden': 'remove',
    },
    initialize: function () {
        this.render();
        this.$el.modal();
        this.$('button.active').focus();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
        this.$('button[value=' + this.model.get('priority') + ']').addClass('active');
    },
    submit: function (evt) {
        evt.preventDefault();
        this.$('.sync-status').empty();
        this.$('.modal-footer button').button('loading');
        var priority = this.$('button.active').val();
        this.model.save({priority: priority}, {patch: true, wait: true})
            .done(_.bind(this.save_success, this))
            .fail(_.bind(this.save_error, this));
    },
    save_success: function (response, status, xhr) {
        this.$el.modal('hide');
    },
    save_error: function (xhr) {
        alert_for_xhr(xhr).appendTo(this.$('.sync-status'));
        this.$('.modal-footer button').button('reset');
    },
});

var RecipeSetCancelModal = Backbone.View.extend({
    tagName: 'div',
    className: 'modal',
    template: JST['recipeset-cancel'],
    events: {
        'submit form': 'submit',
        'hidden': 'remove',
    },
    initialize: function () {
        this.render();
        this.$el.modal();
        this.$('[name=message]').focus();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
    },
    submit: function (evt) {
        evt.preventDefault();
        this.$('.sync-status').empty();
        this.$('.modal-footer button').button('loading');
        var message = this.$('[name=message]').val();
        this.model.cancel(message)
            .done(_.bind(this.save_success, this))
            .fail(_.bind(this.save_error, this));
    },
    save_success: function (response, status, xhr) {
        this.$el.modal('hide');
    },
    save_error: function (xhr) {
        alert_for_xhr(xhr).appendTo(this.$('.sync-status'));
        this.$('.modal-footer button').button('reset');
    },
});

var RecipeSetWaiveModal = Backbone.View.extend({
    tagName: 'div',
    className: 'modal',
    template: JST['recipeset-waive'],
    events: {
        'submit form': 'submit',
        'hidden': 'remove',
    },
    initialize: function () {
        this.render();
        this.$el.modal();
        this.$('[name=comment]').focus();
    },
    render: function () {
        this.$el.html(this.template(this.model.attributes));
    },
    submit: function (evt) {
        evt.preventDefault();
        this.$('.sync-status').empty();
        this.$('.modal-footer button').button('loading');
        var comment = this.$('[name=comment]').val();
        this.model.waive(comment)
            .done(_.bind(this.save_success, this))
            .fail(_.bind(this.save_error, this));
    },
    save_success: function (response, status, xhr) {
        this.$el.modal('hide');
    },
    save_error: function (xhr) {
        alert_for_xhr(xhr).appendTo(this.$('.sync-status'));
        this.$('.modal-footer button').button('reset');
    },
});

})();
