//! Test-only adapter for the external eager Rust/C++ wrapper.
use geodesk_rs::{GeoDesk,BoundingBox};
use std::{fs,time::Instant,hint::black_box};
use serde_json::json;
fn mix(mut x:u64)->u64{x=(x^(x>>30)).wrapping_mul(0xbf58476d1ce4e5b9);x=(x^(x>>27)).wrapping_mul(0x94d049bb133111eb);x^(x>>31)}
fn main()->Result<(),Box<dyn std::error::Error>>{
 let a:Vec<_>=std::env::args().collect();let samples:usize=a[3].parse()?;let target:f64=a[4].parse::<f64>()?*1e6;let start=Instant::now();let lib=GeoDesk::open(&a[1])?;let open=start.elapsed().as_nanos();
 for line in fs::read_to_string(&a[2])?.lines(){let s:Vec<_>=line.split('\t').collect();let name=s[0];let op=s[1];eprintln!("BEGIN {name}");
 let run=||->Result<serde_json::Value,Box<dyn std::error::Error>>{
  if op=="geometry"{return Err("unsupported_equivalent_geometry_payload: wrapper exposes rings/nodes, not general geometry".into());}
  let bbox=BoundingBox::new(s[7].parse()?,s[8].parse()?,s[9].parse()?,s[10].parse()?);
  // query() eagerly populates C++ FeatureData. to_vec() copies it into Rust.
  // Both MUST be timed; QueryResult::count() is only a cached vector length.
  let execute=||->Result<(u64,u64),Box<dyn std::error::Error>>{let result=lib.query(s[6],bbox)?.to_vec()?;let mut items=0;for f in &result{if op=="tags"{for t in &f.tags{black_box(t);items+=1;}}else{black_box(f);items+=1;}}Ok((result.len() as u64,items))};
  let start=Instant::now();let expected=execute()?;let first=start.elapsed().as_nanos();execute()?;let start=Instant::now();execute()?;let repeats=(target/start.elapsed().as_nanos().max(1) as f64).clamp(1.,1000.) as usize;let mut wall=vec![];
  for _ in 0..samples{let start=Instant::now();for _ in 0..repeats{assert_eq!(execute()?,expected);}wall.push(start.elapsed().as_nanos() as f64/repeats as f64);}
  let result=lib.query(s[6],bbox)?.to_vec()?;let mut sum=0u64;let mut xor=0u64;for f in &result{let k=if f.is_node(){0}else if f.is_way(){1}else{2};let m=mix((f.id as u64).wrapping_mul(4)+k);sum=sum.wrapping_add(m);xor^=m;}
  Ok(json!({"engine":"rust-ffi","name":name,"operation":op,"count":expected.0,"items":expected.1,"open_ns":open,"first_ns":first,"repeats":repeats,"samples_wall_ns":wall,"fingerprint":{"count":result.len(),"sum":sum.to_string(),"xor":xor.to_string()},"validation_level":"identities_and_consumption_counts","payload":"eager_rich_records","timing_includes_query_construction":true}))};
  match run(){Ok(v)=>println!("{v}"),Err(e)=>println!("{}",json!({"engine":"rust-ffi","name":name,"error":e.to_string()}))}
 }Ok(())
}
