<?php
// Original adapter for the extension's public array API. Query does not expose bbox filtering.
use Zitsky\GeoDesk\Features;
$start=hrtime(true);$library=new Features($argv[1]);$open=hrtime(true)-$start;
$samples=(int)$argv[3];$target=(int)$argv[4]*1000000;
foreach(file($argv[2], FILE_IGNORE_NEW_LINES) as $line){
 $s=explode("\t",$line);$name=$s[0];$op=$s[1];fwrite(STDERR,"BEGIN $name\n");
 try {
  if($s[2]!=="")throw new Exception("unsupported_bbox: public API exposes feature-within/radius but no bbox selection");
  if($op!=="iterate")throw new Exception("unsupported_equivalent_payload");
  $start=hrtime(true);$q=$library->query($s[6]);$setup=hrtime(true)-$start;
  $execute=function()use($q){$n=0;foreach($q->toArray() as $f){$n++;}return $n;};
  $start=hrtime(true);$count=$execute();$first=hrtime(true)-$start;$execute();
  $start=hrtime(true);$execute();$repeats=max(1,min(1000,(int)($target/max(1,hrtime(true)-$start))));$wall=[];
  for($i=0;$i<$samples;$i++){$start=hrtime(true);for($j=0;$j<$repeats;$j++){if($execute()!==$count)throw new Exception("unstable consumption");}$wall[]=(hrtime(true)-$start)/$repeats;}
  $ids=[];foreach($q->toArray() as $f){$ids[]=$f->type().'/'.$f->id();}sort($ids,SORT_STRING);$hash=hash_init('sha256');foreach($ids as $id){hash_update($hash,$id."\n");}
  echo json_encode(['engine'=>'php','name'=>$name,'operation'=>$op,'count'=>$count,'items'=>$count,'open_ns'=>$open,'setup_ns'=>$setup,'first_ns'=>$first,'repeats'=>$repeats,'samples_wall_ns'=>$wall,'ids_sha256'=>hash_final($hash),'validation_level'=>'sorted_identity_sha256','payload'=>'materialized_feature_objects','peak_php_bytes'=>memory_get_peak_usage(true)]),"\n";
 }catch(Throwable $e){echo json_encode(['engine'=>'php','name'=>$name,'error'=>$e->getMessage()]),"\n";}
}
