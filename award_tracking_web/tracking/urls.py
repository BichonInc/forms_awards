from django.urls import path
from . import views

urlpatterns = [
    path('grants/', views.grant_list, name='grant_list'),
    path('grants/create/', views.grant_create, name='grant_create'),
    path('grants/<str:grant_id>/', views.grant_detail, name='grant_detail'),
    path('refresh_gl_expenditure/', views.refresh_gl_expenditure, name='refresh_gl_expenditure'),
    path('refresh_subsequent_adjustment/', views.refresh_subsequent_adjustment, name='refresh_subsequent_adjustment'),  # Add this line
    path('download/', views.download_data_csv, name='download_data_csv'),
]


