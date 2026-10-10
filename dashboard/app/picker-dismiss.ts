'use client';
import {useEffect,useRef} from 'react';
export function usePickerDismiss(){
 const ref=useRef<HTMLDetailsElement>(null);
 useEffect(()=>{
  const outside=(event:PointerEvent)=>{if(ref.current?.open&&!ref.current.contains(event.target as Node))ref.current.open=false;};
  const key=(event:KeyboardEvent)=>{if(event.key==='Escape'&&ref.current?.open){event.preventDefault();event.stopImmediatePropagation();ref.current.open=false;ref.current.querySelector('summary')?.focus();}};
  const focus=(event:FocusEvent)=>{if(ref.current?.open&&!ref.current.contains(event.target as Node))ref.current.open=false;};
  document.addEventListener('pointerdown',outside,true);document.addEventListener('keydown',key,true);document.addEventListener('focusin',focus,true);
  return()=>{document.removeEventListener('pointerdown',outside,true);document.removeEventListener('keydown',key,true);document.removeEventListener('focusin',focus,true);};
 },[]);return ref;
}
