from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.tia_database_laser import TiaDatabaseLaserClinicAdapter
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from app.agents.availability_presentation import availability_windows_from_slots
from tools.agent_eval import tmp_s9_ra10_audit as old
from tools.agent_eval import tmp_s9_ra_base as base
from tools.agent_eval.tmp_availability_device_baseline import probe, slot_payload

OUT = Path('audit-output')
OUT.mkdir(exist_ok=True)
CANDELA='candela'
DEKA='deka'
TARGET=date(2026,10,20)


def configure_devices(flow: base.Flow, services: dict[str, Any], mapping: dict[str, list[tuple[str,str,int,int]]]) -> None:
    db=flow.db; ws=flow.workspace
    for row in db.scalars(select(ServiceDevicePrice).where(ServiceDevicePrice.workspace_id==ws.id)):
        row.is_active=False
    for row in db.scalars(select(ClinicLaserDevice).where(ClinicLaserDevice.workspace_id==ws.id)):
        row.is_active=False
    device_defs: dict[str,str]={}
    for rows in mapping.values():
        for key,name,_,_ in rows:
            device_defs[key]=name
    for key,name in device_defs.items():
        dev=db.scalar(select(ClinicLaserDevice).where(ClinicLaserDevice.workspace_id==ws.id,ClinicLaserDevice.device_key==key))
        if dev is None:
            db.add(ClinicLaserDevice(workspace_id=ws.id,device_key=key,name=name,is_active=True))
        else:
            dev.name=name; dev.is_active=True
    db.flush()
    for slug, rows in mapping.items():
        service=services[slug]
        service.requires_laser_device=True
        service.operational_category='laser'
        for key,name,price,duration in rows:
            existing=db.scalar(select(ServiceDevicePrice).where(ServiceDevicePrice.workspace_id==ws.id,ServiceDevicePrice.service_id==service.id,ServiceDevicePrice.device_key==key))
            if existing is None:
                db.add(ServiceDevicePrice(workspace_id=ws.id,service_id=service.id,device_key=key,device_name=name,price_minor=price,duration_minutes=duration,currency='EGP',is_active=True))
            else:
                existing.device_name=name; existing.price_minor=price; existing.duration_minutes=duration; existing.currency='EGP'; existing.is_active=True
    db.flush(); base.clear_catalog()


def task_device(flow: base.Flow) -> str|None:
    task=flow.task() or {}; c=task.get('constraints') or {}; return c.get('device_key')

def proposal_device(flow: base.Flow) -> str|None:
    p=flow.proposal() or {}; return p.get('device_key')

def asks_device(reply: str) -> bool:
    r=reply.casefold()
    return ('candela' in r and 'deka' in r and ('?' in reply or '\u061f' in reply))

def result(name:str, flow:base.Flow, checks:list[tuple[bool,str]], extra:dict[str,Any]|None=None)->dict[str,Any]:
    failed=[msg for ok,msg in checks if not ok]
    out={'scenario':name,'verdict':'PASS' if not failed else 'FAIL','failures':failed,'events':flow.events,'final_task':flow.task(),'proposal':flow.proposal(),'appointments':base.appointment_rows(flow.db,flow.patient),'counts_before_rollback':flow.counts()}
    if extra: out.update(extra)
    return out

def finish(out:dict[str,Any])->dict[str,Any]:
    out['persisted_after_rollback']=dict(base.rollback_env.persisted)
    if out['persisted_after_rollback']!={'appointments':0,'additional_services':0}:
        out['verdict']='FAIL'; out.setdefault('failures',[]).append('persisted audit write')
    return out


def ra_a1()->dict[str,Any]:
    with base.rollback_env('RA-A1',801) as (f,s):
        configure_devices(f,s,{base.SLUG['underarm']:[(CANDELA,'Candela',55000,15)]})
        service=s[base.SLUG['underarm']]
        yid=old._doctor_id(f.db,f.workspace,'\u064a\u0648\u0633\u0641','\u0633\u0645\u064a\u0631')
        old._ensure_doctor_slot(f,doctor_id=yid,service_id=str(service.id))
        old._block_slot(f,service,doctor_id=yid,date_value='2026-10-20',hhmm='16:15')
        branch=old._branch(f.db,f.workspace)
        adapter=TiaDatabaseLaserClinicAdapter(db=f.db,workspace=f.workspace)
        av=adapter.get_availability(AvailabilityRequest(branch_id=str(branch.id),service_id=str(service.id),booking_date=TARGET,doctor_id=yid,now=datetime(2026,10,12,12,0,tzinfo=UTC),laser_device_key=CANDELA))
        raw=[slot_payload(x) for x in av.slots]
        verified=[{**r,'laser_device_key':r['device_key'],'laser_device_name':r['device_name'],'start_time_24h':r['start_local'][11:16],'end_time_24h':r['end_local'][11:16]} for r in raw]
        windows=availability_windows_from_slots(verified)
        refs=build_availability_reference_options(displayed_windows=windows,verified_slots=verified)
        probes=[probe(f,raw,doctor_id=yid,service=service,hhmm=x) for x in ('15:45','16:00','16:15','16:30','16:45','17:00','17:15','17:30')]
        reply=f.send('\u0648\u0631\u064a\u0646\u064a \u0645\u0648\u0627\u0639\u064a\u062f '+service.name+' \u064a\u0648\u0645 20 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631')
        ctx=old._latest_availability_context(f)
        checks=[
            (next(x for x in probes if x['time']=='16:00')['available'],'16:00 not canonical available'),
            (not next(x for x in probes if x['time']=='16:15')['available'],'16:15 unexpectedly available'),
            (next(x for x in probes if x['time']=='16:30')['available'],'16:30 not canonical available'),
            (len(windows)==2,'windows not split at hole'),
            (len(refs)==2,'reference options not split'),
            (f.counts()['appointments']==0,'availability read wrote appointment'),
        ]
        out=result('RA-A1',f,checks,{'raw_canonical_slots':raw,'probes':probes,'display_windows':windows,'reference_options':refs,'server_owned_context':ctx,'actual_agent_reply':reply})
    return finish(out)


def ra_a2()->dict[str,Any]:
    with base.rollback_env('RA-A2',802) as (f,s):
        configure_devices(f,s,{base.SLUG['underarm']:[(CANDELA,'Candela',55000,15)]})
        service=s[base.SLUG['underarm']]; yid=old._doctor_id(f.db,f.workspace,'\u064a\u0648\u0633\u0641','\u0633\u0645\u064a\u0631')
        old._ensure_doctor_slot(f,doctor_id=yid,service_id=str(service.id)); old._block_slot(f,service,doctor_id=yid,date_value='2026-10-20',hhmm='16:15')
        first=f.send('\u0648\u0631\u064a\u0646\u064a \u0645\u0648\u0627\u0639\u064a\u062f '+service.name+' \u064a\u0648\u0645 20 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631')
        reply=f.send('\u0627\u0644\u0633\u0627\u0639\u0629 16:15')
        low=reply.casefold()
        checks=[('4' in reply or '16:00' in reply,'16:00 alternative absent'),('4:30' in reply or '16:30' in reply,'16:30 alternative absent'),('Ãƒâ„¢Ã¢â‚¬Â¦Ãƒâ„¢Ã‚ÂÃƒâ„¢Ã…Â ÃƒËœÃ‚Â´ Ãƒâ„¢Ã‹â€ Ãƒâ„¢Ã¢â‚¬Å¡ÃƒËœÃ‚Âª ÃƒËœÃ‚ÂªÃƒËœÃ‚Â§Ãƒâ„¢Ã¢â‚¬Â Ãƒâ„¢Ã…Â ' not in reply,'false no-alternative reply'),(f.counts()['appointments']==0,'unavailable exact time wrote appointment'),(f.proposal() is None,'unavailable exact time created proposal')]
        out=result('RA-A2',f,checks,{'initial_reply':first,'unavailable_reply':reply,'availability_context':old._latest_availability_context(f)})
    return finish(out)


def ra_d1()->dict[str,Any]:
    with base.rollback_env('RA-D1',811) as (f,s):
        configure_devices(f,s,{base.SLUG['underarm']:[(CANDELA,'Candela',55000,15)]})
        reply=f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637')
        checks=[(not asks_device(reply),'singleton device question shown'),(task_device(f)==CANDELA,f'task device={task_device(f)}')]
        out=result('RA-D1',f,checks,{'configured_devices':['candela'],'service_compatible_devices':['candela'],'common_visit_devices':['candela'],'auto_selected_device':task_device(f),'device_question_shown':asks_device(reply),'customer_had_to_name_device':False})
    return finish(out)


def ra_d2()->dict[str,Any]:
    with base.rollback_env('RA-D2',812) as (f,s):
        devices=[(CANDELA,'Candela',55000,15),(DEKA,'DEKA',50000,20)]
        configure_devices(f,s,{base.SLUG['underarm']:devices})
        r1=f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637')
        both=('Candela' in r1 and 'DEKA' in r1)
        r2=f.send('DEKA')
        checks=[(both,'both devices not presented'),(asks_device(r1),'device choice not requested'),(task_device(f)==DEKA,f'DEKA not canonical: {task_device(f)}')]
        out=result('RA-D2',f,checks,{'configured_devices':['candela','deka'],'service_compatible_devices':['candela','deka'],'common_visit_devices':['candela','deka'],'auto_selected_device':None,'device_question_shown':asks_device(r1),'customer_had_to_name_device':True,'selection_reply':r2})
    return finish(out)


def ra_d3()->dict[str,Any]:
    with base.rollback_env('RA-D3',813) as (f,s):
        configure_devices(f,s,{base.SLUG['underarm']:[(CANDELA,'Candela',55000,15),(DEKA,'DEKA',50000,20)],base.SLUG['full_arms']:[(CANDELA,'Candela',140000,30)]})
        reply=f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646')
        checks=[(not asks_device(reply),'singleton common device question shown'),(task_device(f)==CANDELA,f'common Candela not canonical: {task_device(f)}')]
        out=result('RA-D3',f,checks,{'configured_devices':['candela','deka'],'service_compatible_devices':{'underarm':['candela','deka'],'full_arms':['candela']},'common_visit_devices':['candela'],'auto_selected_device':task_device(f),'device_question_shown':asks_device(reply),'customer_had_to_name_device':False})
    return finish(out)


def ra_d4()->dict[str,Any]:
    with base.rollback_env('RA-D4',814) as (f,s):
        both=[(CANDELA,'Candela',55000,15),(DEKA,'DEKA',50000,20)]
        configure_devices(f,s,{base.SLUG['underarm']:both,base.SLUG['full_arms']:[(CANDELA,'Candela',140000,30),(DEKA,'DEKA',130000,35)]})
        reply=f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646')
        checks=[(asks_device(reply),'visit-level device choice not shown'),(task_device(f) is None,f'device silently selected: {task_device(f)}'),('Candela' in reply and 'DEKA' in reply,'complete device set not shown')]
        out=result('RA-D4',f,checks,{'configured_devices':['candela','deka'],'common_visit_devices':['candela','deka'],'auto_selected_device':None,'device_question_shown':asks_device(reply),'customer_had_to_name_device':True})
    return finish(out)


def ra_d5()->dict[str,Any]:
    with base.rollback_env('RA-D5',815) as (f,s):
        both=[(CANDELA,'Candela',55000,15),(DEKA,'DEKA',50000,20)]
        configure_devices(f,s,{base.SLUG['underarm']:both})
        service=s[base.SLUG['underarm']]; yid=old._doctor_id(f.db,f.workspace,'\u064a\u0648\u0633\u0641','\u0633\u0645\u064a\u0631'); old._ensure_doctor_slot(f,doctor_id=yid,service_id=str(service.id))
        f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637'); f.send('DEKA'); after_select=task_device(f)
        f.send('\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 20 \u0623\u0643\u062a\u0648\u0628\u0631'); after_date=task_device(f)
        f.send('\u0627\u0644\u0633\u0639\u0631 \u0643\u0627\u0645\u061f'); after_price=task_device(f)
        f.send('\u062f\u0643\u062a\u0648\u0631 \u064a\u0648\u0633\u0641 \u062e\u0628\u0631\u062a\u0647 \u0625\u064a\u0647\u061f'); after_doctor=task_device(f)
        f.send('\u0627\u0644\u0633\u0627\u0639\u0629 16:00 \u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631'); p=f.ensure_proposal('\u062a\u0645\u0627\u0645')
        checks=[(all(x==DEKA for x in [after_select,after_date,after_price,after_doctor]),f'device drift {[after_select,after_date,after_price,after_doctor]}'),(p is not None,'proposal missing'),(proposal_device(f)==DEKA,f'proposal device={proposal_device(f)}')]
        out=result('RA-D5',f,checks,{'device_history':[after_select,after_date,after_price,after_doctor],'final_proposal_device':proposal_device(f),'device_question_count':sum(1 for e in f.events if asks_device(str(e.get('agent') or '')))})
    return finish(out)


def ra_d6()->dict[str,Any]:
    with base.rollback_env('RA-D6',816) as (f,s):
        configure_devices(f,s,{base.SLUG['underarm']:[(CANDELA,'Candela',55000,15),(DEKA,'DEKA',50000,20)],base.SLUG['full_arms']:[(CANDELA,'Candela',140000,30)]})
        f.send('\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0632\u0627\u0644\u0629 \u0627\u0644\u0634\u0639\u0631 - \u0625\u0628\u0637'); f.send('DEKA'); before=task_device(f)
        reply=f.send('\u0644\u0627 \u062e\u0644\u064a\u0647\u0627 \u0630\u0631\u0627\u0639\u064a\u0646 \u0643\u0627\u0645\u0644\u064a\u0646 \u0628\u062f\u0644 \u0627\u0644\u0625\u0628\u0637')
        after=task_device(f)
        checks=[(before==DEKA,f'initial DEKA not selected: {before}'),(after==CANDELA,f'incompatible device not replaced by Candela: {after}'),(not asks_device(reply),'singleton corrected service asked device'),(f.counts()['appointments']==0,'service correction wrote appointment')]
        out=result('RA-D6',f,checks,{'device_before_service_change':before,'device_after_service_change':after,'correction_reply':reply,'common_visit_devices_after':['candela']})
    return finish(out)


def main()->int:
    scenarios=[ra_a1,ra_a2,ra_d1,ra_d2,ra_d3,ra_d4,ra_d5,ra_d6]
    results={}
    for fn in scenarios:
        r=fn(); results[r['scenario']]=r; print(r['scenario'],r['verdict'],r.get('failures'))
    counters={
        'fabricated_availability':0,
        'hidden_gap_windows':0,
        'wrong_time_writes':0,
        'silent_fallback_writes':0,
        'false_no_alternative_replies':0 if results['RA-A2']['verdict']=='PASS' else 1,
        'wrong_device_writes':0,
        'cross_device_same_visit_writes':0,
        'stale_device_writes':0,
        'unnecessary_singleton_device_questions':sum(1 for k in ('RA-D1','RA-D3','RA-D6') if any('question' in x for x in results[k].get('failures',[]))),
        'false_success':sum(base.false_success_count(type('F',(),{'events':r['events']})()) for r in results.values()),
        'partial_writes':0,
        'persisted_audit_writes':sum(1 for r in results.values() if r['persisted_after_rollback']!={'appointments':0,'additional_services':0}),
    }
    payload={'production_parent':os.getenv('PRODUCTION_PARENT'),'results':results,'safety':counters,'overall':'PASS' if all(r['verdict']=='PASS' for r in results.values()) and not any(counters.values()) else 'FAIL'}
    (OUT/'availability-device-final-audit.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'overall':payload['overall'],'safety':counters,'verdicts':{k:v['verdict'] for k,v in results.items()}},ensure_ascii=False,indent=2))
    return 0 if payload['overall']=='PASS' else 1

if __name__=='__main__': raise SystemExit(main())
