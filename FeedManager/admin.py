from django.contrib import admin
from .models import ProcessedFeed, OriginalFeed, Filter, Article, AppSetting
from django.utils.html import format_html
from django.urls import reverse
from .forms import FilterForm, ReadOnlyArticleForm
from django.contrib.auth.models import User, Group
from django.core.management import call_command

def update_selected_feeds(modeladmin, request, queryset):
    for feed in queryset:
        call_command('update_feeds', feed=feed.id)
        modeladmin.message_user(request, f"Updated feed: {feed.name}")

def clean_selected_feeds_articles(modeladmin, request, queryset):
    for feed in queryset:
        call_command('clean_old_articles', feed=feed.id)
        modeladmin.message_user(request, f"Cleaned old articles from feed: {feed.title}")

clean_selected_feeds_articles.short_description = "Clean old articles for selected feeds"
update_selected_feeds.short_description = "Update selected feeds"

class FilterInline(admin.TabularInline):
    model = Filter
    form = FilterForm
    extra = 0

class ArticleInline(admin.TabularInline):
    model = Article
    form = ReadOnlyArticleForm
    extra = 0
    readonly_fields = [field.name for field in Article._meta.fields if field.name != 'content']

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

class ProcessedFeedAdmin(admin.ModelAdmin):
    inlines = [FilterInline]
    list_display = ('name', 'articles_to_summarize_per_interval', 'subscription_link')
#    filter_horizontal = ('feeds',)
    search_fields = ('name', 'feeds__title', 'feeds__url')
    list_filter = ('articles_to_summarize_per_interval', 'summary_language', 'model')
    actions = [update_selected_feeds]
    autocomplete_fields = ['feeds']  

    def subscription_link(self, obj):
        url = reverse('processed_feed_by_name', args=[obj.name])
        auth_code = AppSetting.get_auth_code()  # Get the universal auth code
        if not auth_code:
            return format_html('<a href="{}">Subscribe</a>', url)
        return format_html('<a href="{}?key={}">Subscribe</a>', url, auth_code)
    
    subscription_link.short_description = "Subscribe Link"


class OriginalFeedAdmin(admin.ModelAdmin):
    inlines = [ArticleInline]
    search_fields = ('title', 'url')
    actions = [clean_selected_feeds_articles]

    
admin.site.register(ProcessedFeed, ProcessedFeedAdmin)
admin.site.register(OriginalFeed, OriginalFeedAdmin)

@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ['auth_code']
    fields = ['auth_code']

admin.site.unregister(User)
admin.site.unregister(Group)