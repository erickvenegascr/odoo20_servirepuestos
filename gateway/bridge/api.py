from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as drf_exception_handler
from .serializers import SubmissionSerializer, OperationSerializer, EventSerializer
from . import services

def exception_handler(exc, context):
    if isinstance(exc, RequestDataTooBig):
        return Response({'detail': 'Request exceeds size limit'}, status=413)
    if isinstance(exc, DatabaseError):
        return Response({'detail': 'Gateway persistence unavailable'}, status=503)
    return drf_exception_handler(exc, context)

@require_GET
def live(request):
    return JsonResponse({'status': 'ok'})

@require_GET
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return JsonResponse({'status': 'ready'})
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)

class OperationCollection(APIView):
    def post(self, request):
        serializer = SubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        op, created = services.submit(serializer.validated_data, request.user)
        op = services.process_operation(op.pk)
        return Response(OperationSerializer(op).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get('limit', 50)), 1), 100)
        except ValueError:
            raise ValidationError('limit must be an integer')
        qs = services.scope(request.user).order_by('-created_at', '-id')
        if request.query_params.get('state'):
            qs = qs.filter(state=request.query_params['state'])
        return Response({'results': OperationSerializer(qs[:limit], many=True).data})

class OperationDetail(APIView):
    def get(self, request, pk):
        op = get_object_or_404(services.scope(request.user), pk=pk)
        return Response(OperationSerializer(op).data)

class OperationEvents(APIView):
    def get(self, request, pk):
        op = get_object_or_404(services.scope(request.user), pk=pk)
        try:
            after = max(0, int(request.query_params.get('after', 0)))
        except ValueError:
            raise ValidationError('after must be an integer')
        events = list(op.events.filter(pk__gt=after)[:200])
        return Response({'results': EventSerializer(events, many=True).data,
                         'next_after': events[-1].pk if events else after})

class OperationRetry(APIView):
    def post(self, request, pk):
        get_object_or_404(services.scope(request.user), pk=pk)
        op = services.retry(pk, request.user)
        return Response(OperationSerializer(services.process_operation(op.pk)).data)
