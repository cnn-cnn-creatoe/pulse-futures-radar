'use client';
// A custom combobox needs styled options and consistent keyboard behavior, unlike native datalist.
/* oxlint-disable jsx-a11y/prefer-tag-over-role */
import { useId, useState } from 'react';

export type Contract = { symbol: string; name: string; active?: boolean };
export function ContractPicker({ items, value, multiple, onChange }: { items: Contract[]; value: string; multiple: boolean; onChange: (value: string) => void }) {
  const id = useId();
  const [open, setOpen] = useState(false), [active, setActive] = useState(0);
  const tokens = value.split(/[\s,，;；]+/), query = (multiple ? tokens.at(-1) || '' : value).trim().toUpperCase();
  const selected = multiple ? tokens.slice(0,-1) : [];
  const matches = items.filter(c => !selected.includes(c.symbol) && (!query || (c.symbol+' '+c.name).toUpperCase().includes(query)))
    .sort((a,b) => (Number(b.name.toUpperCase() === query)*4+Number(b.symbol === query)*8+Number(b.symbol.startsWith(query)))-(Number(a.name.toUpperCase() === query)*4+Number(a.symbol === query)*8+Number(a.symbol.startsWith(query))) || a.symbol.localeCompare(b.symbol)).slice(0,12);
  const choose = (symbol: string) => { onChange(multiple ? [...selected,symbol].join(' ')+' ' : symbol); setOpen(false); setActive(0); };
  return <div className="wb-contract" onBlur={e => { if(!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}>
    <input aria-label="合约代码或名称" role="combobox" aria-expanded={open} aria-controls={id} aria-autocomplete="list" aria-activedescendant={open && matches[active] ? `${id}-${active}` : undefined}
      autoComplete="off" value={value} placeholder={multiple ? '输入代码或名称，空格分隔' : '输入代码或名称'} onFocus={() => setOpen(true)}
      onChange={e => { onChange(e.target.value.toUpperCase()); setOpen(true); setActive(0); }}
      onKeyDown={e => { if(e.nativeEvent.isComposing) return; if(e.key === 'Escape') setOpen(false); if(e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setOpen(true); setActive(n => Math.max(0,Math.min(matches.length-1,n+(e.key === 'ArrowDown' ? 1 : -1)))); } if(e.key === 'Enter' && open && matches[active]) { e.preventDefault(); choose(matches[active].symbol); } }}/>
    {open && <div id={id} role="listbox" aria-label="匹配合约" className="wb-contract-options">{matches.map((c,i) => <button type="button" role="option" aria-selected={i===active} id={`${id}-${i}`} key={c.symbol} onMouseDown={e => e.preventDefault()} onClick={() => choose(c.symbol)}><b>{c.symbol}</b><span>{c.name}{c.active === false ? ' · 已保存历史' : ''}</span></button>)}{!matches.length && <p>未找到合约，请输入完整代码或等待合约列表更新</p>}</div>}
  </div>;
}
