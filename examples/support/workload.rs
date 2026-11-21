#![forbid(unsafe_code)]
use regol::{Feature, FeatureId, FeatureType, Result, TagValue, source::Source};
use std::hint::black_box;

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Work {
    pub features: u64,
    pub items: u64,
    pub sum: u64,
    pub xor: u64,
}
fn mix(mut x: u64) -> u64 {
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    x ^ (x >> 31)
}
fn typed(id: FeatureId) -> u64 {
    id.id.wrapping_mul(4)
        + match id.kind {
            FeatureType::Node => 0,
            FeatureType::Way => 1,
            FeatureType::Relation => 2,
        }
}
fn bytes(mut hash: u64, value: &[u8]) -> u64 {
    for &byte in value {
        hash = (hash ^ u64::from(byte)).wrapping_mul(1099511628211);
    }
    hash
}
impl Work {
    fn item<const VERIFY: bool>(&mut self, parent: u64, value: u64) {
        self.items += 1;
        if VERIFY {
            let hash = mix(parent ^ mix(value));
            self.sum = self.sum.wrapping_add(hash);
            self.xor ^= hash;
        }
    }
}
/// Keep unlimited iteration's hot loop unchanged; limited workloads measure
/// ordinary iterator consumption and early release of the remaining iterator.
pub fn consume_limited<'a, S: Source + 'a>(
    iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    operation: &str,
    limit: Option<usize>,
) -> Result<Work> {
    match limit {
        Some(limit) => consume::<_, false>(iter.take(limit), operation),
        None => consume::<_, false>(iter, operation),
    }
}

/// Validation hashes are deliberately outside timed runs. Both paths still
/// consume the same fields; VERIFY only adds deterministic content hashing.
pub fn consume<'a, S: Source + 'a, const VERIFY: bool>(
    mut iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    operation: &str,
) -> Result<Work> {
    if !VERIFY && operation == "iterate" {
        let features = iter.try_fold(0u64, |n, feature| -> Result<u64> {
            black_box(feature?);
            Ok(n + 1)
        })?;
        return Ok(Work {
            features,
            items: features,
            ..Work::default()
        });
    }
    let mut work = Work::default();
    for feature in iter {
        let feature = feature?;
        work.features += 1;
        let parent = if VERIFY { typed(feature.id()) } else { 0 };
        match operation {
            "iterate" => {
                black_box(feature);
                work.item::<VERIFY>(parent, parent);
            }
            "tags" => {
                for tag in feature.tags()? {
                    let tag = black_box(tag?);
                    let mut hash = 0;
                    if VERIFY {
                        hash = bytes(14695981039346656037, tag.key.as_bytes());
                        hash = bytes(hash, &[255]);
                        hash = match &tag.value {
                            TagValue::Text(value) => bytes(bytes(hash, b"T"), value.as_bytes()),
                            value => bytes(
                                bytes(hash, b"N"),
                                &value.as_number().unwrap().to_bits().to_le_bytes(),
                            ),
                        };
                    }
                    work.item::<VERIFY>(parent, hash);
                }
            }
            "positions" if feature.kind() == FeatureType::Relation => {}
            "positions" if feature.kind() == FeatureType::Node => {
                if let Some(p) = black_box(feature.point()?) {
                    work.item::<VERIFY>(
                        parent,
                        (u64::from(p.x as u32) << 32) | u64::from(p.y as u32),
                    );
                }
            }
            "coordinates" | "positions" => {
                for point in feature.coordinates()? {
                    let point = black_box(point?);
                    work.item::<VERIFY>(
                        parent,
                        (u64::from(point.x as u32) << 32) | u64::from(point.y as u32),
                    );
                }
            }
            "way_nodes" => {
                for (index, node) in feature.way_nodes()?.enumerate() {
                    let node = black_box(node?);
                    let id = node.id.ok_or_else(|| {
                        std::io::Error::other("way_nodes workload requires retained OSM node IDs")
                    })?;
                    let xy =
                        (u64::from(node.point.x as u32) << 32) | u64::from(node.point.y as u32);
                    work.item::<VERIFY>(
                        parent,
                        if VERIFY {
                            mix(index as u64 ^ mix(id)) ^ xy
                        } else {
                            0
                        },
                    );
                }
            }
            "elevation" | "peak_fields" => {
                let elevation = black_box(feature.tag("ele")?);
                let mut hash = 0;
                if VERIFY {
                    hash = match elevation {
                        Some(TagValue::Text(s)) => bytes(1, s.as_bytes()),
                        Some(v) => v.as_number().unwrap().to_bits(),
                        None => 0,
                    };
                }
                if operation == "peak_fields" {
                    for key in ["name:cs", "name:en", "name"] {
                        let name = match black_box(feature.tag(key)?) {
                            Some(TagValue::Text(name)) if !name.trim().is_empty() => name,
                            _ => continue,
                        };
                        if VERIFY {
                            hash = bytes(hash, name.as_bytes());
                        }
                        break;
                    }
                    if let Some(p) = black_box(feature.point()?) {
                        hash ^= (u64::from(p.x as u32) << 32) | u64::from(p.y as u32);
                    }
                }
                work.item::<VERIFY>(parent, hash);
            }
            "members" => {
                for member in feature.members()? {
                    let member = black_box(member?);
                    let value = if VERIFY {
                        typed(member.feature.id())
                            ^ bytes(14695981039346656037, member.role.as_bytes())
                    } else {
                        0
                    };
                    work.item::<VERIFY>(parent, value);
                }
            }
            "geometry" => {
                let geometry = black_box(feature.geometry()?);
                work.item::<VERIFY>(parent, if VERIFY { geometry_hash(&geometry) } else { 0 });
            }
            "parents" => {
                for parent_feature in feature.parent_relations()? {
                    let parent_feature = black_box(parent_feature?);
                    work.item::<VERIFY>(
                        parent,
                        if VERIFY {
                            typed(parent_feature.id())
                        } else {
                            0
                        },
                    );
                }
            }
            _ => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "unknown benchmark operation",
                )
                .into());
            }
        }
    }
    Ok(work)
}

// Geometry verification runs outside timing. Normalize ring start/orientation,
// line orientation and component order, while preserving coordinate adjacency,
// polygon/hole grouping, repeated vertices, geometry types and multiplicity.
fn sequence_hash(kind: u8, points: &[(i32, i32)], start: usize) -> u64 {
    let mut hash = bytes(14695981039346656037, &[kind]);
    hash = bytes(hash, &(points.len() as u64).to_le_bytes());
    for i in 0..points.len() {
        let (x, y) = points[(start + i) % points.len()];
        hash = bytes(hash, &x.to_le_bytes());
        hash = bytes(hash, &y.to_le_bytes());
    }
    hash
}
fn least_rotation(points: &[(i32, i32)]) -> usize {
    let n = points.len();
    let (mut i, mut j, mut k) = (0, 1, 0);
    while i < n && j < n && k < n {
        match points[(i + k) % n].cmp(&points[(j + k) % n]) {
            std::cmp::Ordering::Equal => {
                k += 1;
                continue;
            }
            std::cmp::Ordering::Greater => {
                i += k + 1;
                if i <= j {
                    i = j + 1;
                }
            }
            std::cmp::Ordering::Less => {
                j += k + 1;
                if j <= i {
                    j = i + 1;
                }
            }
        }
        k = 0;
    }
    i.min(j)
}
fn path_hash(points: &[regol::Point], ring: bool) -> u64 {
    let mut points: Vec<_> = points.iter().map(|p| (p.x, p.y)).collect();
    if ring && points.first() == points.last() {
        points.pop();
    }
    let kind = if ring { b'R' } else { b'L' };
    let forward = sequence_hash(
        kind,
        &points,
        if ring { least_rotation(&points) } else { 0 },
    );
    points.reverse();
    let reverse = sequence_hash(
        kind,
        &points,
        if ring { least_rotation(&points) } else { 0 },
    );
    forward.min(reverse)
}
fn components_hash(kind: u8, mut values: Vec<u64>) -> u64 {
    values.sort_unstable();
    let mut hash = bytes(14695981039346656037, &[kind]);
    hash = bytes(hash, &(values.len() as u64).to_le_bytes());
    for value in values {
        hash = bytes(hash, &value.to_le_bytes());
    }
    hash
}
fn polygon_hash(polygon: &regol::Polygon) -> u64 {
    let exterior = path_hash(&polygon.exterior, true);
    let holes = components_hash(
        b'H',
        polygon
            .interiors
            .iter()
            .map(|ring| path_hash(ring, true))
            .collect(),
    );
    let hash = bytes(14695981039346656037, b"A");
    bytes(bytes(hash, &exterior.to_le_bytes()), &holes.to_le_bytes())
}
fn geometry_hash(geometry: &regol::Geometry) -> u64 {
    use regol::Geometry;
    match geometry {
        Geometry::Point(p) => sequence_hash(b'P', &[(p.x, p.y)], 0),
        Geometry::LineString(points) => path_hash(points, false),
        Geometry::Polygon(polygon) => polygon_hash(polygon),
        Geometry::MultiPolygon(polygons) => {
            components_hash(b'M', polygons.iter().map(polygon_hash).collect())
        }
        Geometry::Collection(children) => {
            components_hash(b'C', children.iter().map(geometry_hash).collect())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use regol::{Geometry, Point, Polygon};
    fn points(values: &[(i32, i32)]) -> Vec<Point> {
        values.iter().map(|&(x, y)| Point { x, y }).collect()
    }
    #[test]
    fn least_rotation_matches_exhaustive_small_cycles() {
        for n in 1..=8 {
            for bits in 0..(1usize << n) {
                let points: Vec<_> = (0..n).map(|i| (((bits >> i) & 1) as i32, 0)).collect();
                let canonical =
                    |start: usize| (0..n).map(|i| points[(start + i) % n]).collect::<Vec<_>>();
                assert_eq!(
                    canonical(least_rotation(&points)),
                    (0..n).map(canonical).min().unwrap()
                );
            }
        }
    }
    #[test]
    fn geometry_fingerprints_normalize_only_immaterial_order() {
        // Repeated least vertices exercise rotation ties, not just rectangles.
        let ring = points(&[(0, 0), (2, 0), (0, 0), (0, 2), (0, 0)]);
        let expected = path_hash(&ring, true);
        for reverse in [false, true] {
            for offset in 0..ring.len() - 1 {
                let mut shifted = ring[..ring.len() - 1].to_vec();
                if reverse {
                    shifted.reverse();
                }
                shifted.rotate_left(offset);
                shifted.push(shifted[0]);
                assert_eq!(path_hash(&shifted, true), expected);
            }
        }
        let a = points(&[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]);
        let crossed = points(&[(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)]);
        assert_ne!(path_hash(&a, true), path_hash(&crossed, true));
        let hole = points(&[(0, 0), (1, 0), (1, 1), (0, 0)]);
        let polygon = Polygon {
            exterior: a.clone(),
            interiors: vec![hole.clone()],
        };
        let swapped = Polygon {
            exterior: hole,
            interiors: vec![a.clone()],
        };
        assert_ne!(polygon_hash(&polygon), polygon_hash(&swapped));
        assert_ne!(
            geometry_hash(&Geometry::LineString(a.clone())),
            geometry_hash(&Geometry::Polygon(Polygon {
                exterior: a,
                interiors: vec![]
            }))
        );
        // Shared cross-language golden values are verified by the Python helper.
        assert_eq!(
            geometry_hash(&Geometry::Point(Point { x: -17, y: 29 })),
            13595356716078976439
        );
    }
}
