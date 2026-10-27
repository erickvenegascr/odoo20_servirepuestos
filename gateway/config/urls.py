from django.urls import path
from bridge import api
urlpatterns = [
    path('health/live/', api.live), path('health/ready/', api.ready),
    path('api/v1/operations/', api.OperationCollection.as_view()),
    path('api/v1/operations/<uuid:pk>/', api.OperationDetail.as_view()),
    path('api/v1/operations/<uuid:pk>/events/', api.OperationEvents.as_view()),
    path('api/v1/operations/<uuid:pk>/retry/', api.OperationRetry.as_view()),
]
