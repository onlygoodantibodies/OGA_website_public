from django.contrib import admin
from .models import Lesson, Quiz, Question, Answer, Certificate

class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 4  # Shows 4 answer fields per question

class QuestionAdmin(admin.ModelAdmin):
    inlines = [AnswerInline]

# academy/admin.py

from django.contrib import admin
from .models import Lesson, LessonSection

class LessonSectionInline(admin.StackedInline):
    model = LessonSection
    extra = 1

@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ("order", "title")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [LessonSectionInline]


admin.site.register(Quiz)
admin.site.register(Question, QuestionAdmin)
admin.site.register(Certificate)