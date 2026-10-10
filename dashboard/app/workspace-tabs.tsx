'use client';
import {Children,useState,type ReactNode} from 'react';
export function WorkspaceTabs({labels,children,footer}:{labels:string[];children:ReactNode;footer?:ReactNode}){
 const [active,setActive]=useState(0);const panes=Children.toArray(children).filter(child=>typeof child!=='string'||child.trim().length>0);
 return <div className="workspace-tabs"><div className="terminal-tabs" role="tablist" aria-label="工作区分类">{labels.map((label,i)=><button key={label} role="tab" aria-selected={active===i} onClick={()=>setActive(i)}>{label}</button>)}</div><div className="workspace-panes">{panes.map((pane,i)=><div className="workspace-pane" role="tabpanel" aria-label={labels[i]} hidden={active!==i} key={labels[i]||i}>{pane}</div>)}</div>{footer&&<div className="workspace-footer">{footer}</div>}</div>;
}
