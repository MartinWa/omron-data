from django.contrib.auth import views as auth_views
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from .context_processors import app_info
from .forms import LoginForm


class LoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        return response


def not_found(request, exception):
    return render(request, "404.html", status=404)


def server_error(request):
    user = getattr(request, "user", None) or AnonymousUser()
    context = {**app_info(None), "user": user}
    return HttpResponse(render_to_string("500.html", context, request=request), status=500)
