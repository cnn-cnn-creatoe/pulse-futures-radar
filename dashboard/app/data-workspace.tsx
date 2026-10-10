'use client';
import {useState} from 'react';
import {DownloadPanel} from './download-panel';
import {UniversePanel} from './monitor-panels';
export function DataWorkspace({api,online}:{api:string|null;online:boolean}){
 const [tab,setTab]=useState('download');
 return <div className="data-terminal"><div className="terminal-tabs"><button aria-pressed={tab==='download'} onClick={()=>setTab('download')}>下载进度</button><button aria-pressed={tab==='universe'} onClick={()=>setTab('universe')}>合约列表 / 上线提醒</button></div><div className="data-terminal-content">{tab==='download'?<DownloadPanel api={api} online={online}/>:<UniversePanel api={api} online={online}/>}</div></div>;
}
