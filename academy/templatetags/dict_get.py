from django import template
register = template.Library()

@register.filter
def get(d, key):
    """
    Usage in template:
      {{ answers|get:"<question_id>" }}
    returns answers.get('question_<question_id>')
    """
    return d.get(f'question_{key}')
