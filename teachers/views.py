#!/usr/bin/env python
# -*- coding: utf-8 -*-
# -*- coding: utf8 -*-

import os
import datetime

from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.views.generic import View
from django.shortcuts import render
from django.utils import simplejson

from diogenis.teachers.models import *
from diogenis.students.models import *
from diogenis.schools.models import *

from diogenis.common.decorators import request_passes_test, cache_view
from diogenis.teachers.mixins import AuthenticatedTeacherMixin
from diogenis.teachers.helpers import pdf_exporter

def user_is_teacher(request, username=None, **kwargs):
    try:
        user = request.user
        teacher = Teacher.objects.get(user=user)
        if username:
            return user.is_authenticated() and username == user.username
        return user.is_authenticated()
    except:
        return False

@request_passes_test(user_is_teacher, login_url="/login/")
@cache_view(48*60*60)
def manage_labs(request, username):
    '''
    Manages teacher's views.
    
    Handling Templates: /teachers/labs.html | /teachers/pending-students.html
    '''
    #import ipdb; ipdb.set_trace();
    pending_students_request = request.path.endswith('pending-students/')        #[Boolean] checking current url path
    teacher = Teacher.objects.get(user=request.user)
    courses = teacher.get_courses_by_school()
    
    #####################################################################
    # Builds the context making related dicts/lists with lesson names,
    # teacher's labs, and the registered students for each lab.
    #####################################################################        
    results = []
    labs = Lab.objects.filter(teacher=teacher, start_hour__gt=1).order_by('course__lesson__name', 'start_hour').select_related('course__lesson__name', 'classroom__name')
    
    labs_context = []
    for lab in labs:
        if pending_students_request:
            subscriptions = Subscription.objects.filter(lab=lab, in_transit=True).order_by('student').select_related()
        else:
            subscriptions = Subscription.objects.filter(lab=lab, in_transit=False).order_by('student').select_related()
        
        students = []
        for subscription in subscriptions:
            students.append({
                            'first':subscription.student.user.first_name,
                            'last':subscription.student.user.last_name,
                            'am':subscription.student.am,
                            'subscription_id':subscription.hash_id,
                            'absences':subscription.opinionated_absences,
                            'id':subscription.student.hash_id
                            })
        
        sibling_labs = lab.sibling_labs if not pending_students_request else lab.sibling_labs_plus_self
        sibling_labs_context = {}
        sibling_labs_context['owners'] = map(get_sibling_context, sibling_labs['owners'])
        sibling_labs_context['others'] = map(get_sibling_context, sibling_labs['others'])
        
        labs_context.append({
                        'id':lab.hash_id,
                        'lesson': {'name':lab.course.lesson.name},
                        'classroom': {'name':lab.classroom.name},
                        'day':lab.day,
                        'hour':lab.hour,
                        'students':students,
                        'sibling_labs':sibling_labs_context,
                        'empty_seats':lab.empty_seats
                        })
        
    context =   {
                'labs':labs_context,
                'courses':courses['context']
                }
    template = ('teachers/pending_students.html' if pending_students_request else 'teachers/labs.html')
    return render(request, template, context)
    
def get_sibling_context(lab):
    return {'id':lab.hash_id,
            'name':lab.classroom.name,
            'day':lab.day[:3],
            'hour':lab.hour,
            'students':{'registered':lab.registered_students_count, 'max':lab.max_students}
            }

@request_passes_test(user_is_teacher, login_url="/login/")
def submit_student_to_lab(request):
    '''
    Manages JSON request for transferring student(s) across lesson-specific labs.
    
    Client-side: [js/core.teachers.lab.transfer.js]
    '''
    if request.method == "POST" and request.is_ajax():
        json_data = simplejson.loads(request.raw_post_data)
        data = {}
        try:
            lab =   {
                    'new':json_data['lab']['new'],
                    'old':json_data['lab']['old']
                    }
        except KeyError:
            msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = { "status": 2, "msg": msg }
        
        lab['new'] = Lab.objects.get(hash_id=lab['new']['id'])
        lab['old'] = Lab.objects.get(hash_id=lab['old']['id'])
        
        try:
            students = json_data['students']
            empty_test = students[0]        #raises an error students dict is empty
            for student in students:
                student = Student.objects.get(hash_id=student['id'])
                subscription = Subscription.objects.get(student=student, lab=lab['old'])
                subscription.lab = lab['new']
                available = subscription.check_availability()        #checks student's availability in order to be transferred.
                if available:
                    subscription.in_transit = False
                    subscription.save()
                else:
                    msg = u"Κάποιοι σπουδαστές έχουν δηλώσει άλλα εργαστήρια αυτές τις ώρες"
                    data = { "status": 3, "msg": msg }
        except KeyError:
            msg = u"Δεν έχετε επιλέξει κάποιον σπουδαστή"
            data = { "status": 3, "msg": msg }
        
        if not data:
            ok_msg = u"Η μεταφορά στο εργαστήριο %s ολοκληρώθηκε" % subscription.lab.classroom.name
            data = { "status": 1, "msg": ok_msg }
        data = simplejson.dumps(data)
        return HttpResponse(data, mimetype='application/javascript')

@request_passes_test(user_is_teacher, login_url="/login/")
def delete_subscription(request):
    if request.method == "POST" and request.is_ajax():
        json_data = simplejson.loads(request.raw_post_data)
        data = {}
        try:
            action = json_data['action']
            students = json_data['students']
        except KeyError:
            msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = {'status':2, 'msg':msg}
        
        hashes = map(lambda x:x['subscription_id'], students)
        subscriptions = Subscription.objects.filter(hash_id__in=hashes)
        subscriptions.delete()
        
        if not data:
            ok_msg = u"Η διαγραφή των εγγραφών ολοκληρώθηκε"
            data = { "status": 1, "msg": ok_msg }
        data = simplejson.dumps(data)
        return HttpResponse(data, mimetype='application/javascript')

@request_passes_test(user_is_teacher, login_url="/login/")
def delete_lab(request, username, hash_id):
    lab = Lab.objects.get(hash_id=hash_id)
    lab.delete()
    return HttpResponseRedirect('/teachers/%s/' % username)
    

@request_passes_test(user_is_teacher, login_url="/login/")
def add_new_lab(request):
    '''
    Manages JSON request for creating a new lab.
    
    Client-side: [js/core.teachers.lab.register.js]
    '''
    if request.method == "POST" and request.is_ajax():
        json_data = simplejson.loads(request.raw_post_data)
        data = {}
        hour = {}
        
        teacher = Teacher.objects.get(user=request.user)
        try:
            action = json_data['action']
            course_id = json_data['course_id']
            day = json_data['day']
            hour['start'] = json_data['hour']['start']
            hour['end'] = json_data['hour']['end']
        except KeyError:
            msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = {'status':2, 'msg':msg}
        
        course = Course.objects.get(hash_id = course_id)
        
        if action == "classes":        #returns the available classes
            if hour['start'] >= hour['end']:
                msg = u"H ώρα έναρξης του εργαστηρίου είναι μεγαλύτερη της ώρας λήξης"
                data = {'status':2, 'action':action, 'msg':msg}
            else:
                classes = []
                school = School.objects.get(hash_id=course.school.hash_id)
                classrooms = school.classrooms.all()
                for classroom in classrooms:
                    classes.append({ 'name':classroom.name, 'id':classroom.hash_id })
                data = {'status':1, 'action':action, 'classes':classes}
        
        elif action == "submit":        #saves new lab
            try:
                classroom_id = json_data['classroom_id']
                max_students = json_data['max_students']
            except KeyError:
                msg = u"Δεν επιλέξατε αίθουσα εργαστηρίου"
                data = {'status':2, 'action':action, 'msg':msg}
            
            classroom = Classroom.objects.get(hash_id=classroom_id)
            teacher = Teacher.objects.get(user=request.user)
            
            try:
                lab = Lab(course=course, classroom=classroom, teacher=teacher, day=day, max_students=max_students)
                lab.hour = hour
                lab.save()
                msg = u"Η προσθήκη ολοκληρώθηκε"
                data = {'status':1, 'action':action, 'msg':msg}
            except ValidationError, e:
                msg =  e.messages[0]
                data = {'status':2, 'action':action, 'msg':msg}
                
        if not data:
            error_msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = {'status':2, 'action':action, 'msg':error_msg}
        data = simplejson.dumps(data)
        return HttpResponse(data, mimetype='application/javascript')


@request_passes_test(user_is_teacher, login_url="/login/")
def update_absences(request):
    '''
    Manages JSON request for updating student absences.
    
    Client-side: [js/core.teachers.absences.js]
    '''
    if request.method == "POST" and request.is_ajax():
        json_data = simplejson.loads(request.raw_post_data)
        data = {}
        try:
            action = json_data['action']
            subscription_id = json_data['subscription']['id']
        except KeyError:
            msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = {'status':2, 'msg':msg}
        
        subscription = Subscription.objects.get(hash_id = subscription_id)
        
        if action == "add":
            subscription.absences += 1
            subscription.save()
            data = {'status':1, 'action':action, 'absences_context':subscription.opinionated_absences}
        
        elif action == "remove":
            subscription.absences -= 1
            subscription.save()
            data = {'status':1, 'action':action, 'absences_context':subscription.opinionated_absences}
            
        if not data:
            error_msg = u"Παρουσιάστηκε σφάλμα κατά την αποστολή των δεδομένων"
            data = {'status':2, 'action':action, 'msg':error_msg}
        data = simplejson.dumps(data)
        return HttpResponse(data, mimetype='application/javascript')


@request_passes_test(user_is_teacher, login_url="/login/")
def export_pdf(request, hash_id):
    '''
    ###
    # Needs to be documented by Lomar
    ###
    '''
    if request.method == "GET":
        lab = Lab.objects.get(hash_id=hash_id)
        
        teacher = Teacher.objects.get(user=request.user)
        
        date = datetime.datetime.now()
        filename = str('pdf_report.%s.pdf') % (date)
        filename = unicode(filename,"utf-8")
        
        response = HttpResponse(mimetype='application/pdf')
        response['Content-Disposition'] = 'attachment; filename=%s' % filename
        pdf_exporter(lab,response)
        return response


class SettingsView(AuthenticatedTeacherMixin, View):
    
    def get(self, request, username):
        #self.school = School.objects.get(user=request.user)
        return render(request, 'teachers/settings.html', {})
        
    def post(self, request, username):
        user = request.user
        user.set_password(request.POST['password'])
        user.save()
        
        message = {'status':1, 'msg':u'Η αλλαγή κωδικού στον Διογένη ολοκληρώθηκε'}
        context =   {
                    'message':message,
                    }
        return render(request, 'teachers/settings.html', context)
        

settings = SettingsView.as_view()
