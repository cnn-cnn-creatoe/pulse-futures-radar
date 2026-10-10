import {useRef,useLayoutEffect,useEffect} from 'react';

// Keep each mounted workspace's scroll positions when navigation hides it.
export function usePreservedScroll<T extends HTMLElement>(active:boolean){
 const ref=useRef<T>(null);
 const positions=useRef(new Map<HTMLElement,{top:number;left:number}>());
 const visible=useRef(active);
 useLayoutEffect(()=>{
  visible.current=active;
  if(active)for(const [node,p] of positions.current){if(node.isConnected){node.scrollTop=p.top;node.scrollLeft=p.left;}}
 },[active]);
 useEffect(()=>{
  const root=ref.current;if(!root)return;
  const remember=(e:Event)=>{if(!visible.current||root.closest('[hidden]'))return;const node=e.target;if(node instanceof HTMLElement)positions.current.set(node,{top:node.scrollTop,left:node.scrollLeft});};
  root.addEventListener('scroll',remember,true);
  return()=>root.removeEventListener('scroll',remember,true);
 },[]);
 return ref;
}
