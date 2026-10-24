from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .models import User, Center
from django.db import transaction


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def assign_center_to_admin(request):
    """
    Assign a center to a user (admin, teacher, or student).
    
    POST /api/auth/assign-center/
    {
        "user_id": "user_id_or_email",
        "center_id": "center_id_or_uuid"
    }
    
    Only super admins can assign centers to users.
    """
    user = request.user
    
    # Check if user is super admin, platform owner, or manager
    if (user.role or '').lower() not in ['super_admin', 'superadmin', 'platform_owner', 'manager']:
        return Response(
            {'error': 'Only super admins or platform owners can assign centers to admins'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    user_id = request.data.get('user_id')
    center_id = request.data.get('center_id')
    
    if not user_id or not center_id:
        return Response(
            {'error': 'user_id and center_id are required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Get the admin user
        try:
            admin_user = User.objects.get(id=user_id)
        except (User.DoesNotExist, ValueError):
            # Try by email
            admin_user = User.objects.get(email=user_id)
        
        # Get the center - use tenant DB
        from accounts.utils import get_current_db
        current_db = get_current_db() or 'default'
        try:
            center = Center.objects.using(current_db).get(id=center_id)
        except (Center.DoesNotExist, ValueError):
            center = Center.objects.using(current_db).get(name=center_id)
        
        # Check if the user role is valid for center assignment
        # Allow all roles except system-level ones
        invalid_roles = []  # Allow all roles to be assigned to centers
        if admin_user.role.lower() in invalid_roles:
            return Response(
                {'error': f'User {admin_user.email} cannot be assigned to a center (current role: {admin_user.role})'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Assign center to admin
        with transaction.atomic():
            admin_user.center = center
            admin_user.save()
            
            # Also add to center's admins many-to-many relationship
            if hasattr(center, 'admins'):
                center.admins.add(admin_user)
        
        return Response({
            'message': f'Successfully assigned {admin_user.email} to center {center.name}',
            'user': {
                'id': str(admin_user.id),
                'email': admin_user.email,
                'username': admin_user.username,
                'role': admin_user.role,
            },
            'center': {
                'id': str(center.id),
                'name': center.name,
                'city': center.city,
            }
        })
        
    except User.DoesNotExist:
        return Response(
            {'error': f'User with id/email "{user_id}" not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Center.DoesNotExist:
        return Response(
            {'error': f'Center with id/name "{center_id}" not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def remove_center_from_admin(request):
    """
    Remove center assignment from an admin user.
    
    POST /api/auth/remove-center/
    {
        "user_id": "user_id_or_email"
    }
    """
    user = request.user
    
    # Check if user is super admin, platform owner, or manager
    if (user.role or '').lower() not in ['super_admin', 'superadmin', 'platform_owner', 'manager']:
        return Response(
            {'error': 'Only super admins or platform owners can remove center assignments'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    user_id = request.data.get('user_id')
    
    if not user_id:
        return Response(
            {'error': 'user_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Get the admin user
        try:
            admin_user = User.objects.get(id=user_id)
        except (User.DoesNotExist, ValueError):
            admin_user = User.objects.get(email=user_id)
        
        old_center = admin_user.center
        
        # Remove center assignment
        with transaction.atomic():
            if old_center and hasattr(old_center, 'admins'):
                old_center.admins.remove(admin_user)
            
            admin_user.center = None
            admin_user.save()
        
        return Response({
            'message': f'Successfully removed center assignment from {admin_user.email}',
            'user': {
                'id': str(admin_user.id),
                'email': admin_user.email,
                'username': admin_user.username,
            },
            'previous_center': {
                'id': str(old_center.id) if old_center else None,
                'name': old_center.name if old_center else None,
            } if old_center else None
        })
        
    except User.DoesNotExist:
        return Response(
            {'error': f'User with id/email "{user_id}" not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_available_centers(request):
    """
    Get list of available centers for assignment.
    
    GET /api/auth/available-centers/?institute_id={id}
    """
    user = request.user
    institute_id = request.query_params.get('institute_id')
    
    if not institute_id:
        if hasattr(user, 'institute_id'):
            institute_id = user.institute_id
        elif hasattr(user, 'institute'):
            institute_id = user.institute.id
        else:
            return Response(
                {'error': 'institute_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    try:
        centers = Center.objects.filter(institute_id=institute_id).order_by('name')
        
        return Response({
            'centers': [
                {
                    'id': str(center.id),
                    'name': center.name,
                    'city': center.city,
                    'address': center.address,
                    'admin_count': center.admins.count() if hasattr(center, 'admins') else 0,
                }
                for center in centers
            ]
        })
        
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
